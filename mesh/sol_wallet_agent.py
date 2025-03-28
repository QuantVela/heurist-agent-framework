import asyncio
import json
import logging
import os
from typing import Any, Dict, List

import aiohttp
import pydash as _py
from dotenv import load_dotenv
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from core.llm import call_llm_async, call_llm_with_tools_async
from decorators import monitor_execution, with_cache, with_retry

from .mesh_agent import MeshAgent

logger = logging.getLogger(__name__)
load_dotenv()


class SolWalletAgent(MeshAgent):
    def __init__(self):
        super().__init__()
        self.session = None
        self.api_url = "https://mainnet.helius-rpc.com"

        self.metadata.update(
            {
                "name": "Solana Wallet Agent",
                "version": "1.0.0",
                "author": "QuantVela",
                "author_address": "0x53cc700f818DD0b440598c666De2D630F9d47273",
                "description": "This agent can query Solana wallet assets and recent swap transactions using Helius API.",
                "inputs": [
                    {
                        "name": "query",
                        "description": "Natural language query about Solana wallet assets and transactions",
                        "type": "str",
                        "required": False,
                    },
                    {
                        "name": "raw_data_only",
                        "description": "If true, the agent will only return the raw data without LLM explanation",
                        "type": "bool",
                        "required": False,
                        "default": False,
                    },
                ],
                "outputs": [
                    {
                        "name": "response",
                        "description": "Natural language explanation of the wallet data",
                        "type": "str",
                    },
                    {
                        "name": "data",
                        "description": "Structured wallet data including assets and transactions",
                        "type": "dict",
                    },
                ],
                "external_apis": ["Helius"],
                "tags": ["Solana"],
                "image_url": "https://raw.githubusercontent.com/heurist-network/heurist-agent-framework/refs/heads/main/mesh/images/Solana.png",
                "examples": [
                    "Give me the holders of this token: J7tYmq2JnQPvxyhcXpCDrvJnc9R5ts8rv7tgVHDPsw7U",
                    "Show me the txs of this wallet: DbDi7soBXALYRMZSyJMEAfpaK3rD1hr5HuCYzuDrcEEN",
                    "What tokens does DbDi7soBXALYRMZSyJMEAfpaK3rD1hr5HuCYzuDrcEEN hold?",
                ],
            }
        )

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
            self.session = None

    async def _request(self, method, url, data=None, json=None, headers=None, params=None, timeout=30):
        if not self.session:
            raise RuntimeError("Session not initialized. Use 'async with' context manager.")

        try:
            async with self.session.request(
                method,
                url,
                data=data,
                json=json,
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                match response.status:
                    case 200:
                        return await response.json()
                    case 429:
                        raise aiohttp.ClientError("Rate limit exceeded")
                    case _:
                        # should add better error log
                        txt = await response.text()
                        logger.error(txt)
                        return {}

        except aiohttp.ClientResponseError as e:
            error_msg = f"HTTP error {e.status}: {e.message}"
            raise aiohttp.ClientResponseError(e.request_info, e.history, status=e.status, message=error_msg)
        except aiohttp.ClientError as e:
            raise aiohttp.ClientError(f"Request failed: {str(e)}")

    async def _post(self, url: str, json: dict):
        headers = {"Content-Type": "application/json"}
        return await self._request("POST", url=url, json=json, headers=headers, timeout=10)

    async def _get(self, url: str, params: dict):
        headers = {"Content-Type": "application/json"}
        return await self._request("GET", url=url, params=params, headers=headers, timeout=10)

    def _format_amount(self, amount: int, decimals: int) -> str:
        """Helper function to format token amounts"""
        return str(amount / (10**decimals))

    def get_system_prompt(self) -> str:
        return """You are a Solana blockchain data expert who can access wallet assets and transaction information through the Helius API.

        CAPABILITIES:
        - Query wallet token holdings
        - Analyze token holder patterns
        - View wallet swap transaction history

        RESPONSE GUIDELINES:
        - Keep responses concise and focused on the specific data requested
        - Format monetary values in a readable way (e.g. "$150.4M")
        - Only provide metrics relevant to the query
        - Highlight any anomalies or significant patterns if found
        """

    def get_tool_schemas(self) -> List[Dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "get_sol_wallet_assets",
                    "description": """Query and retrieve all token holdings for a specific Solana wallet address with comprehensive details about each asset.
                    This tool provides a detailed breakdown of wallet contents including SOL balance, token quantities, current market prices, and total value in USD.
                    Use this tool when you need to analyze a wallet's complete portfolio composition, assess the total value of holdings, or identify significant token positions.
                    This tool will NOT provide historical holding data, transaction history, or price predictions - only current snapshot of assets.
                    The results are ordered by value, with more valuable assets appearing first. Some small-value tokens or dust amounts might be excluded from results.
                    The returned data includes token addresses, symbols, images, current price per token, and total value in USD.""",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "owner_address": {
                                "type": "string",
                                "description": "The Solana wallet address to query (must be a valid Solana public key in base58 format)",
                            }
                        },
                        "required": ["owner_address"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "analyze_sol_token_holders",
                    "description": """Analyze the distribution and behavior patterns of top token holders for a specific Solana token.
                    This tool examines concentration of ownership by identifying the largest holders of a token and what other assets these major holders commonly own.
                    Use this when investigating token distribution, whale behavior patterns, correlated investments, or potential market manipulation by major holders.
                    Do NOT use this tool for tokens with extremely high holder counts (>50,000) as results may be incomplete.
                    The analysis excludes certain protocol wallets (like Raydium) that would skew the results and focuses on actual user wallets.
                    Results include:
                    - The percentage of total supply held by each address
                    - Total value of holdings in USD
                    - Interconnections between major holders
                    - Common tokens held across these wallets
                    For each holder, a GMGN explorer link (gmgn_link_owner_address) is provided for easy access to detailed wallet information.
                    The response also includes a GMGN referral link that can be used for further exploration of Solana data.
                    These insights help identify investment patterns and potential coordinated activity among major token holders.""",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "token_address": {
                                "type": "string",
                                "description": "The Solana token mint address to analyze (must be a valid SPL token mint address in base58 format)",
                            },
                            "top_n": {
                                "type": "integer",
                                "description": "Number of top holders to analyze - higher values provide more comprehensive analysis but increase processing time (default: 20, max: 100)",
                                "default": 20,
                            },
                        },
                        "required": ["token_address"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_sol_tx_history",
                    "description": """Fetch and analyze the most recent SWAP transactions for a specific Solana wallet address.
                    This tool provides a chronological record of token swaps, including detailed information about tokens exchanged, amounts, and transaction types (BUY/SELL/SWAP).
                    Use this when you need to understand a wallet's recent trading behavior, identify patterns in swap frequency, analyze asset allocation changes over time, or track specific swapping activities.
                    The tool will NOT return other transaction types like NFT trades, transfers, or staking - it focuses exclusively on token swaps.
                    Results are limited to the 100 most recent swap transactions and include timestamp, transaction type, input/output token addresses, and token amounts.
                    A transaction is classified as BUY when SOL is used to purchase another token, SELL when a token is converted to SOL, and SWAP for token-to-token exchanges.
                    This detailed swap history helps identify trading strategies, entry/exit points, and token preferences of the wallet owner.""",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "owner_address": {
                                "type": "string",
                                "description": "The Solana wallet address to query transaction history for (must be a valid Solana public key in base58 format)",
                            }
                        },
                        "required": ["owner_address"],
                    },
                },
            },
        ]

    # ------------------------------------------------------------------------
    #                       SHARED / UTILITY METHODS
    # ------------------------------------------------------------------------
    async def _respond_with_llm(self, query: str, tool_call_id: str, data: dict, temperature: float) -> str:
        """
        Reusable helper to ask the LLM to generate a user-friendly explanation
        given a piece of data from a tool call.
        """
        return await call_llm_async(
            base_url=self.heurist_base_url,
            api_key=self.heurist_api_key,
            model_id=self.metadata["large_model_id"],
            messages=[
                {"role": "system", "content": self.get_system_prompt()},
                {"role": "user", "content": query},
                {"role": "tool", "content": str(data), "tool_call_id": tool_call_id},
            ],
            temperature=temperature,
        )

    def _handle_error(self, maybe_error: dict) -> dict:
        """
        Small helper to return the error if present in
        a dictionary with the 'error' key.
        """
        if "error" in maybe_error:
            return {"error": maybe_error["error"]}
        return {}

    # ------------------------------------------------------------------------
    #                      HELIUS API-SPECIFIC METHODS
    # ------------------------------------------------------------------------
    @with_cache(ttl_seconds=600)
    @retry(
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
        wait=wait_exponential(multiplier=0.1, min=0, max=10),
        stop=stop_after_attempt(5),
    )
    async def _get_holders(self, token_address: str, top_n: int = 20) -> List[Dict]:
        """
        Query the HELIUS API to get the token top holders for a given token address.
        """
        try:
            logger.info(f"Querying token holders for address: {token_address}")
            all_holders = []
            cursor = None

            while True:
                payload = {
                    "jsonrpc": "2.0",
                    "id": "get-token-accounts-{uuid.uuid4()}",
                    "method": "getTokenAccounts",
                    "params": {"mint": token_address, "limit": 1000, "cursor": cursor},
                }

                data = await self._post(url=f"{self.api_url}/?api-key={os.getenv('HELIUS_API_KEY')}", json=payload)

                if not data.get("result", {}).get("token_accounts"):
                    break

                all_holders.extend(data["result"]["token_accounts"])
                cursor = data["result"].get("cursor")

                if not cursor:
                    break

            if not all_holders:
                return []

            total_supply = sum(float(account["amount"]) for account in all_holders)

            holders = [
                {
                    "address": account["owner"],
                    "amount": float(account["amount"]),
                    "percentage": f"{(float(account['amount']) / total_supply * 100):.2f}",
                }
                for account in all_holders
            ]

            return sorted(holders, key=lambda x: x["amount"], reverse=True)[:top_n]

        except Exception as e:
            logger.error(f"Error querying token holders: {str(e)}")
            return []

    @with_cache(ttl_seconds=600)
    @retry(
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
        wait=wait_exponential(multiplier=0.1, min=0, max=10),
        stop=stop_after_attempt(5),
    )
    async def get_wallet_assets(self, owner_address: str) -> List[Dict]:
        """
        Query the HELIUS API to get the wallet assets for a given owner address.
        """
        try:
            logger.info(f"Querying wallet assets for address: {owner_address}")
            payload = {
                "jsonrpc": "2.0",
                "id": "search-assets-{uuid.uuid4()}",
                "method": "searchAssets",
                "params": {
                    "ownerAddress": owner_address,
                    "tokenType": "fungible",
                    "page": 1,
                    "limit": 100,
                    "sortBy": {"sortBy": "recent_action", "sortDirection": "desc"},
                    "options": {"showNativeBalance": True},
                },
            }

            data = await self._post(url=f"{self.api_url}/?api-key={os.getenv('HELIUS_API_KEY')}", json=payload)

            if data is None:
                return []
            if isinstance(data, dict) and not data.get("result"):
                return []

            # filter assets with price info and total price > 100
            filtered_assets = [
                item
                for item in data["result"]["items"]
                if (
                    item.get("token_info", {}).get("price_info")
                    and item["token_info"]["price_info"].get("total_price", 0) > 100
                )
            ]
            # filter non mutable assets
            non_mutable_assets = [asset for asset in filtered_assets if not asset.get("mutable", False)]

            hold_tokens = []
            # Add native SOL balance if exists
            sol_address = "So11111111111111111111111111111111111111112"
            if native_balance := data["result"].get("nativeBalance"):
                hold_tokens.append(
                    {
                        "token_address": sol_address,
                        "token_img": "",
                        "symbol": "SOL",
                        "price_per_token": native_balance.get("price_per_sol", 0),
                        "total_price": native_balance.get("total_price", 0),
                    }
                )

            # Add other token balances
            hold_tokens.extend(
                [
                    {
                        "token_address": asset["id"],
                        "token_img": (
                            asset.get("content", {}).get("files", [{}])[0]
                            if asset.get("content", {}).get("files")
                            else {}
                        ).get("cdn_uri", ""),
                        "symbol": asset.get("token_info", {}).get("symbol", ""),
                        "price_per_token": asset.get("token_info", {}).get("price_info", {}).get("price_per_token", 0),
                        "total_price": asset.get("token_info", {}).get("price_info", {}).get("total_price", 0),
                    }
                    for asset in non_mutable_assets
                ]
            )

            return hold_tokens

        except Exception:
            # logger.error(f"Error querying HELIUS API: {str(e)}")
            return []

    @with_cache(ttl_seconds=600)
    @retry(
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
        wait=wait_exponential(multiplier=0.1, min=0, max=10),
        stop=stop_after_attempt(5),
    )
    async def analyze_holders(self, token_address: str, top_n: int = 20) -> List[Dict]:
        """
        Analyze the token holders and find what they also hold most.
        """
        try:
            holders = await self._get_holders(token_address, top_n)

            if not holders:
                return []

            raydium_address = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
            top_holders = [h for h in holders if h["address"] != raydium_address]

            tasks = [self.get_wallet_assets(holder["address"]) for holder in top_holders]
            assets_results = await asyncio.gather(*tasks)

            token_map = {}
            for holder, assets in zip(top_holders, assets_results):
                if assets is None or isinstance(assets, dict) and "error" in assets:
                    continue

                for token in assets:
                    token_address = token["token_address"]
                    if token_address not in token_map:
                        token_map[token_address] = {
                            "token_address": token_address,
                            "token_img": token["token_img"],
                            "symbol": token["symbol"],
                            "price_per_token": token["price_per_token"],
                            "total_holding_value": 0,
                            "holders": [],
                        }

                    token_map[token_address]["total_holding_value"] += token["total_price"]
                    token_map[token_address]["holders"].append({
                        "address": holder["address"], 
                        "total_price": token["total_price"],
                        "percentage": holder["percentage"],
                        "gmgn_link_owner_address": f"https://gmgn.ai/sol/address/{holder['address']}"
                    })

            # Sort by total_holding_value and get top 5
            sorted_tokens = sorted(token_map.values(), key=lambda x: x["total_holding_value"], reverse=True)[:5]

            # Sort each token's holders by total_price and get top 5
            for token in sorted_tokens:
                token["holders"] = sorted(token["holders"], key=lambda x: x["total_price"], reverse=True)[:5]
                # Add referral link to each token
                token["gmgn_referral_link"] = "https://gmgn.ai/?ref=WtaAO4Jn&chain=sol"

            logger.info(f"Successfully analyzed holders for token: {token_address}")
            return sorted_tokens

        except Exception as e:
            logger.error(f"Error analyzing holders: {str(e)}")
            return []

    @with_cache(ttl_seconds=600)
    @retry(wait=wait_exponential(multiplier=0.1, min=0, max=10), stop=stop_after_attempt(5))
    async def get_tx_history(self, owner_address: str) -> List[Dict]:
        """
        Query the HELIUS API to get swap transaction history for a given wallet address.
        """

        try:
            logger.info(f"Querying transaction history for address: {owner_address}")

            params = {"api-key": os.getenv("HELIUS_API_KEY"), "type": ["SWAP"], "limit": 100}

            url = f"https://api.helius.xyz/v0/addresses/{owner_address}/transactions"

            data = await self._get(url=url, params=params)

            if not data:
                logger.warning(f"No data returned for address: {owner_address}")
                return []

            swap_txs = []
            SOL_ADDRESS = "So11111111111111111111111111111111111111112"

            if not isinstance(data, list):
                logger.warning(f"Unexpected data format: {type(data)}")
                return []

            swap_type = [tx for tx in data if _py.get(tx, "type") == "SWAP"]

            for tx in swap_type:
                swap_event = _py.get(tx, "events.swap")
                if not swap_event:
                    continue

                processed_data = {
                    "account": _py.get(tx, "feePayer", ""),
                    "timestamp": _py.get(tx, "timestamp", 0),
                    "description": _py.get(tx, "description", ""),
                }

                # Process token_in information
                if _py.get(swap_event, "nativeInput.amount", 0):
                    processed_data.update(
                        {
                            "token_in_address": SOL_ADDRESS,
                            "token_in_amount": self._format_amount(
                                int(_py.get(swap_event, "nativeInput.amount", 0)), 9
                            ),
                        }
                    )
                elif _py.get(swap_event, "tokenInputs"):
                    token_input = _py.get(swap_event, "tokenInputs.0", {})
                    processed_data.update(
                        {
                            "token_in_address": _py.get(token_input, "mint", ""),
                            "token_in_amount": self._format_amount(
                                int(_py.get(token_input, "rawTokenAmount.tokenAmount", 0)),
                                _py.get(token_input, "rawTokenAmount.decimals", 0),
                            ),
                        }
                    )

                # Process token_out information
                if _py.get(swap_event, "nativeOutput.amount", 0):
                    processed_data.update(
                        {
                            "token_out_address": SOL_ADDRESS,
                            "token_out_amount": self._format_amount(
                                int(_py.get(swap_event, "nativeOutput.amount", 0)), 9
                            ),
                        }
                    )
                elif _py.get(swap_event, "tokenOutputs"):
                    token_output = _py.get(swap_event, "tokenOutputs.0", {})
                    processed_data.update(
                        {
                            "token_out_address": _py.get(token_output, "mint", ""),
                            "token_out_amount": self._format_amount(
                                int(_py.get(token_output, "rawTokenAmount.tokenAmount", 0)),
                                _py.get(token_output, "rawTokenAmount.decimals", 0),
                            ),
                        }
                    )

                # Determine transaction type
                if _py.get(processed_data, "token_in_address") == SOL_ADDRESS:
                    processed_data["type"] = "BUY"
                elif _py.get(processed_data, "token_out_address") == SOL_ADDRESS:
                    processed_data["type"] = "SELL"
                else:
                    processed_data["type"] = "SWAP"

                swap_txs.append(processed_data)

            return swap_txs

        except Exception as e:
            logger.error(f"Error querying transaction history: {str(e)}")
            return [{"error": f"Failed to query transaction history: {str(e)}"}]

    # ------------------------------------------------------------------------
    #                      COMMON HANDLER LOGIC
    # ------------------------------------------------------------------------
    async def _handle_tool_logic(self, tool_name: str, function_args: dict) -> Dict[str, Any]:
        if tool_name == "get_sol_wallet_assets":
            result = await self.get_wallet_assets(function_args["owner_address"])
        elif tool_name == "analyze_sol_token_holders":
            result = await self.analyze_holders(function_args["token_address"], function_args.get("top_n", 20))
        elif tool_name == "get_sol_tx_history":
            result = await self.get_tx_history(function_args["owner_address"])
        else:
            return {"error": f"Unsupported tool: {tool_name}"}

        error = self._handle_error(result)
        if error:
            return error

        return result

    # ------------------------------------------------------------------------
    #                      MAIN HANDLER
    # ------------------------------------------------------------------------
    @monitor_execution()
    @with_retry(max_retries=3)
    async def handle_message(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Either 'query' or 'tool' is required in params.
          - If 'tool' is provided, call that tool directly with 'tool_arguments' (bypassing the LLM).
          - If 'query' is provided, route via LLM for dynamic tool selection.
        """
        session_created = False
        if not self.session:
            self.session = aiohttp.ClientSession()
            session_created = True
        try:
            query = params.get("query")
            tool_name = params.get("tool")
            tool_args = params.get("tool_arguments", {})
            raw_data_only = params.get("raw_data_only", False)

            # ---------------------
            # 1) DIRECT TOOL CALL
            # ---------------------
            if tool_name:
                data = await self._handle_tool_logic(tool_name=tool_name, function_args=tool_args)
                return {"response": "", "data": data}

            # ---------------------
            # 2) NATURAL LANGUAGE QUERY (LLM decides the tool)
            # ---------------------
            if query:
                response = await call_llm_with_tools_async(
                    base_url=self.heurist_base_url,
                    api_key=self.heurist_api_key,
                    model_id=self.metadata["large_model_id"],
                    system_prompt=self.get_system_prompt(),
                    user_prompt=query,
                    temperature=0.1,
                    tools=self.get_tool_schemas(),
                )

                if not response:
                    return {"error": "Failed to process query"}

                if not response.get("tool_calls"):
                    # No tool calls => the LLM just answered
                    return {"response": response["content"], "data": {}}

                tool_call = response["tool_calls"]
                tool_call_name = tool_call.function.name
                tool_call_args = json.loads(tool_call.function.arguments)

                data = await self._handle_tool_logic(tool_name=tool_call_name, function_args=tool_call_args)

                if raw_data_only:
                    return {"response": "", "data": data}

                explanation = await self._respond_with_llm(
                    query=query, tool_call_id=tool_call.id, data=data, temperature=0.7
                )
                return {"response": explanation, "data": data}

            return {"error": "Either 'query' or 'tool' must be provided in the parameters."}
        except Exception as e:
            logger.error(f"Error handling message: {str(e)}")
            return {"error": f"Error handling message: {str(e)}"}
        finally:
            # Clean up the session if we created it
            if session_created and self.session:
                await self.session.close()
            self.session = None
