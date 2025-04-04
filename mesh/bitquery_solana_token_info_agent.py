import datetime
import json
import os
from typing import Any, Dict, List

import requests
from dotenv import load_dotenv

from core.llm import call_llm_async, call_llm_with_tools_async
from decorators import monitor_execution, with_cache, with_retry

from .mesh_agent import MeshAgent

load_dotenv()


class BitquerySolanaTokenInfoAgent(MeshAgent):
    def __init__(self):
        super().__init__()
        self.metadata.update(
            {
                "name": "Solana Token Info Agent",
                "version": "1.0.0",
                "author": "Heurist team",
                "author_address": "0x7d9d1821d15B9e0b8Ab98A058361233E255E405D",
                "description": "This agent can analyze the market data of any Solana token, and get trending tokens on Solana",
                "inputs": [
                    {
                        "name": "query",
                        "description": "Natural language query about a Solana token or a request for trending tokens. If you want to query a specific token, you MUST include token address",
                        "type": "str",
                        "required": True,
                    },
                    {
                        "name": "raw_data_only",
                        "description": "If true, the agent will only return the raw data and not the full response",
                        "type": "bool",
                        "required": False,
                        "default": False,
                    },
                ],
                "outputs": [
                    {
                        "name": "response",
                        "description": "Natural language explanation of the token trading information or trending tokens",
                        "type": "str",
                    },
                    {
                        "name": "data",
                        "description": "Structured token trading data or trending tokens data",
                        "type": "dict",
                    },
                ],
                "external_apis": ["Bitquery"],
                "tags": ["Solana", "Trading"],
                "image_url": "https://raw.githubusercontent.com/heurist-network/heurist-agent-framework/refs/heads/main/mesh/images/Solana.png",
                "examples": [
                    "Analyze trending tokens on Solana",
                    "Get token info for HeLp6NuQkmYB4pYWo2zYs22mESHXPQYzXbB8n4V98jwC",
                    "Show top 10 most active tokens on Solana network"
                ],
            }
        )

    def get_system_prompt(self) -> str:
        return (
            "You are a specialized assistant that analyzes Solana token trading data from Bitquery. Your responses should be clear, concise, and data-driven.\n\n"
            "If some data is missing, you don't need to mention it in your report unless it's critical to answer the user's question."
            "Don't be verbose. Present the essential information. You are not a repeater of the raw data but you want to capture the essence and present insights."
            "For any token contract address, you MUST use this format [Mint Address](https://solscan.io/token/Mint_Address)"
            "Use natural language to write your response. You don't need to include how you got or derived the data."
            "Answer user's question based on the provided data. If you don't have enough information, just say you don't know."
        )

    def get_tool_schemas(self) -> List[Dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "get_token_trading_info",
                    "description": "Get detailed token trading information using Solana mint address. This tool fetches trading data including volume, price movements, and liquidity for any Solana token. Use this when you need to analyze a specific Solana token's market performance. Data comes from Bitquery API and only works with valid Solana token addresses.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "token_address": {"type": "string", "description": "The Solana token mint address"}
                        },
                        "required": ["token_address"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_top_trending_tokens",
                    "description": "Get the current top trending tokens on Solana. This tool retrieves a list of the most popular and actively traded tokens on Solana. It provides key metrics for each trending token including price, volume, and recent price changes. Use this when you want to discover which tokens are currently gaining attention in the Solana ecosystem. Data comes from Bitquery API and is updated regularly.",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
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
    #                      API-SPECIFIC METHODS
    # ------------------------------------------------------------------------

    @with_cache(ttl_seconds=300)  # Cache for 5 minutes
    async def get_token_trading_info(self, token_address: str) -> Dict:
        try:
            trading_data = fetch_and_organize_dex_trade_data(token_address)
            if trading_data:
                latest_data = trading_data[-1]
                first_data = trading_data[0]

                price_change = latest_data["close"] - first_data["open"]
                price_change_percent = (price_change / first_data["open"]) * 100 if first_data["open"] != 0 else 0
                total_volume = sum(bucket["volume"] for bucket in trading_data)

                summary = {
                    "current_price": latest_data["close"],
                    "price_change_1h": price_change,
                    "price_change_percentage_1h": price_change_percent,
                    "highest_price_1h": max(bucket["high"] for bucket in trading_data),
                    "lowest_price_1h": min(bucket["low"] for bucket in trading_data),
                    "total_volume_1h": total_volume,
                    "last_updated": datetime.datetime.utcnow().isoformat(),
                }

                return {"summary": summary, "detailed_data": trading_data}
            return {"error": "No trading data available"}
        except Exception as e:
            return {"error": f"Failed to fetch token trading info: {str(e)}"}

    @with_cache(ttl_seconds=300)  # Cache for 5 minutes
    async def get_top_trending_tokens(self) -> Dict:
        """
        Calls the helper function to fetch the top ten trending tokens on the Solana network.
        """
        try:
            trending_tokens = top_ten_trending_tokens()
            return {"trending_tokens": trending_tokens}
        except Exception as e:
            return {"error": f"Failed to fetch top trending tokens: {str(e)}"}

    # ------------------------------------------------------------------------
    #                      COMMON HANDLER LOGIC
    # ------------------------------------------------------------------------
    async def _handle_tool_logic(self, tool_name: str, function_args: dict) -> Dict[str, Any]:
        """
        A single method that calls the appropriate function, handles errors/formatting
        """
        if tool_name == "get_token_trading_info":
            result = await self.get_token_trading_info(function_args["token_address"])
        elif tool_name == "get_top_trending_tokens":
            result = await self.get_top_trending_tokens()
        else:
            return {"error": f"Unsupported tool: {tool_name}"}

        errors = self._handle_error(result)
        if errors:
            return errors

        return result

    @monitor_execution()
    @with_retry(max_retries=3)
    async def handle_message(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle both direct tool calls and natural language queries.
        Either 'query' or 'tool' must be provided in params.
        """
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

            # Check if tool_calls exists and is not None
            tool_calls = response.get("tool_calls")
            if not tool_calls:
                return {"response": response.get("content", "No response content"), "data": {}}

            # Make sure we're accessing the first tool call correctly
            if isinstance(tool_calls, list) and len(tool_calls) > 0:
                tool_call = tool_calls[0]
            else:
                tool_call = tool_calls  # If it's not a list, use it directly

            # Safely extract function name and arguments
            tool_call_name = tool_call.function.name
            tool_call_args = json.loads(tool_call.function.arguments)

            data = await self._handle_tool_logic(tool_name=tool_call_name, function_args=tool_call_args)

            if raw_data_only:
                return {"response": "", "data": data}

            explanation = await self._respond_with_llm(
                query=query, tool_call_id=tool_call.id, data=data, temperature=0.7
            )
            return {"response": explanation, "data": data}

        # ---------------------
        # 3) NEITHER query NOR tool
        # ---------------------
        return {"error": "Either 'query' or 'tool' must be provided in the parameters."}


def fetch_and_organize_dex_trade_data(base_address: str) -> List[Dict]:
    """
    Fetches DEX trade data from Bitquery for the given base token address,
    setting the time_ago parameter to one hour before the current UTC time,
    and returns a list of dictionaries representing time buckets.

    Args:
        base_address (str): The token address for the base token.

    Returns:
        list of dict: Each dictionary contains keys: 'time', 'open', 'high',
                      'low', 'close', 'volume'.
    """
    # Calculate time_ago as one hour before the current UTC time.
    time_ago = (datetime.datetime.utcnow() - datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # GraphQL query using list filtering for tokens.
    query = """
    query (
      $tokens: [String!],
      $base: String,
      $dataset: dataset_arg_enum,
      $time_ago: DateTime,
      $interval: Int
    ) {
      Solana(dataset: $dataset) {
        DEXTradeByTokens(
          orderBy: { ascendingByField: "Block_Time" }
          where: {
            Transaction: { Result: { Success: true } },
            Trade: {
              Side: {
                Amount: { gt: "0" },
                Currency: { MintAddress: { in: $tokens } }
              },
              Currency: { MintAddress: { is: $base } }
            },
            Block: { Time: { after: $time_ago } }
          }
        ) {
          Block {
            Time(interval: { count: $interval, in: minutes })
          }
          min: quantile(of: Trade_PriceInUSD, level: 0.05)
          max: quantile(of: Trade_PriceInUSD, level: 0.95)
          close: median(of: Trade_PriceInUSD)
          open: median(of: Trade_PriceInUSD)
          volume: sum(of: Trade_Side_AmountInUSD)
        }
      }
    }
    """

    # Set up the variables for the query.
    variables = {
        "tokens": [
            "So11111111111111111111111111111111111111112",  # wSOL
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
            "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
        ],
        "base": base_address,
        "dataset": "combined",
        "time_ago": time_ago,
        "interval": 5,
    }

    # Bitquery GraphQL endpoint and headers.
    url = "https://streaming.bitquery.io/eap"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {os.getenv('BITQUERY_API_KEY')}"}

    # Send the POST request.
    response = requests.post(url, json={"query": query, "variables": variables}, headers=headers)
    if response.status_code != 200:
        raise Exception(f"Query failed with status code {response.status_code}: {response.text}")

    raw_data = response.json()

    try:
        buckets = raw_data["data"]["Solana"]["DEXTradeByTokens"]
    except (KeyError, TypeError):
        raise Exception("Unexpected data format received from the API.")

    organized_data = []
    for bucket in buckets:
        time_bucket = bucket.get("Block", {}).get("Time")
        open_price = bucket.get("open")
        high_price = bucket.get("max")
        low_price = bucket.get("min")
        close_price = bucket.get("close")
        volume_str = bucket.get("volume", "0")

        try:
            volume = float(volume_str)
        except ValueError:
            volume = volume_str  # Fallback to the original value if conversion fails.

        organized_data.append(
            {
                "time": time_bucket,
                "open": open_price,
                "high": high_price,
                "low": low_price,
                "close": close_price,
                "volume": volume,
            }
        )

    organized_data.sort(key=lambda x: x["time"])
    return organized_data


def top_ten_trending_tokens():
    """
    Fetches trade summary data from Bitquery using the provided GraphQL query,
    and organizes the returned data into a list of dictionaries for the latest 1-hour data.

    Returns:
        list of dict: Each dictionary contains:
            - currency: { Name, MintAddress, Symbol } of the traded asset.
            - price: { start, min5, end } price data.
            - dex: { ProtocolName, ProtocolFamily, ProgramAddress } details.
            - market: { MarketAddress }.
            - side_currency: { Name, MintAddress, Symbol } from the trade side.
            - makers: int, count of distinct transaction signers.
            - total_trades: int, count of trades.
            - total_traded_volume: float, total traded volume in USD.
            - total_buy_volume: float, total buy volume in USD.
            - total_sell_volume: float, total sell volume in USD.
            - total_buys: int, count of buy trades.
            - total_sells: int, count of sell trades.

    Raises:
        Exception: If the API request fails or the returned data format is not as expected.
    """

    # Calculate the time one hour ago in ISO format.
    time_since = (datetime.datetime.utcnow() - datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Define the GraphQL query with the dynamic time filter.
    query = (
        """
    query MyQuery {
      Solana {
        DEXTradeByTokens(
          where: {
            Transaction: {Result: {Success: true}},
            Trade: {Side: {Currency: {MintAddress: {is: "So11111111111111111111111111111111111111112"}}}},
            Block: {Time: {since: "%s"}}
          }
          orderBy: {descendingByField: "total_trades"}
          limit: {count: 10}
        ) {
          Trade {
            Currency {
              Name
              MintAddress
              Symbol
            }
            start: PriceInUSD(minimum: Block_Time)
            min5: PriceInUSD(
              minimum: Block_Time,
              if: {Block: {Time: {after: "2024-08-15T05:14:00Z"}}}
            )
            end: PriceInUSD(maximum: Block_Time)
            Dex {
              ProtocolName
              ProtocolFamily
              ProgramAddress
            }
            Market {
              MarketAddress
            }
            Side {
              Currency {
                Symbol
                Name
                MintAddress
              }
            }
          }
          makers: count(distinct:Transaction_Signer)
          total_trades: count
          total_traded_volume: sum(of: Trade_Side_AmountInUSD)
          total_buy_volume: sum(
            of: Trade_Side_AmountInUSD,
            if: {Trade: {Side: {Type: {is: buy}}}}
          )
          total_sell_volume: sum(
            of: Trade_Side_AmountInUSD,
            if: {Trade: {Side: {Type: {is: sell}}}}
          )
          total_buys: count(if: {Trade: {Side: {Type: {is: buy}}}})
          total_sells: count(if: {Trade: {Side: {Type: {is: sell}}}})
        }
      }
    }
    """
        % time_since
    )

    # Bitquery endpoint and headers.
    url = "https://streaming.bitquery.io/eap"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {os.getenv('BITQUERY_API_KEY')}"}

    # Execute the HTTP POST request.
    response = requests.post(url, json={"query": query}, headers=headers)
    if response.status_code != 200:
        raise Exception(f"Query failed with status code {response.status_code}: {response.text}")

    raw_data = response.json()

    try:
        trade_summaries = raw_data["data"]["Solana"]["DEXTradeByTokens"]
    except (KeyError, TypeError) as err:
        raise Exception("Unexpected data format received from the API.") from err

    organized_data = []
    # Process each trade summary item.
    for summary in trade_summaries:
        trade_info = summary.get("Trade", {})
        currency = trade_info.get("Currency", {})
        dex = trade_info.get("Dex", {})
        market = trade_info.get("Market", {})
        side = trade_info.get("Side", {}).get("Currency", {})

        # Parse numeric summary fields.
        try:
            makers = int(summary.get("makers", 0))
        except (ValueError, TypeError):
            makers = 0

        try:
            total_trades = int(summary.get("total_trades", 0))
        except (ValueError, TypeError):
            total_trades = 0

        def to_float(val):
            try:
                return float(val)
            except (ValueError, TypeError):
                return 0.0

        total_traded_volume = to_float(summary.get("total_traded_volume", 0))
        total_buy_volume = to_float(summary.get("total_buy_volume", 0))
        total_sell_volume = to_float(summary.get("total_sell_volume", 0))

        try:
            total_buys = int(summary.get("total_buys", 0))
        except (ValueError, TypeError):
            total_buys = 0

        try:
            total_sells = int(summary.get("total_sells", 0))
        except (ValueError, TypeError):
            total_sells = 0

        organized_item = {
            "currency": {
                "Name": currency.get("Name"),
                "MintAddress": currency.get("MintAddress"),
                "Symbol": currency.get("Symbol"),
            },
            "price": {"start": trade_info.get("start"), "min5": trade_info.get("min5"), "end": trade_info.get("end")},
            "dex": {
                "ProtocolName": dex.get("ProtocolName"),
                "ProtocolFamily": dex.get("ProtocolFamily"),
                "ProgramAddress": dex.get("ProgramAddress"),
            },
            "market": {"MarketAddress": market.get("MarketAddress")},
            "side_currency": {
                "Name": side.get("Name"),
                "MintAddress": side.get("MintAddress"),
                "Symbol": side.get("Symbol"),
            },
            "makers": makers,
            "total_trades": total_trades,
            "total_traded_volume": total_traded_volume,
            "total_buy_volume": total_buy_volume,
            "total_sell_volume": total_sell_volume,
            "total_buys": total_buys,
            "total_sells": total_sells,
        }
        organized_data.append(organized_item)

    return organized_data
