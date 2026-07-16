"""Asynchronous weather lookup tool."""
from typing import Optional

import httpx

from infra.logger import get_logger
from tools.base import Tool, ToolParam, ToolResult, ToolSchema


class WeatherTool(Tool):
    """Query simple weather data for a city."""

    name = "weather_query"
    description = "Query weather information for a given city"
    params = [
        ToolParam(name="city", type="string", required=True, description="City name"),
        ToolParam(name="date", type="string", required=False, description="Date hint"),
    ]

    def __init__(self):
        self.logger = get_logger()
        self.base_url = "https://wttr.in"
        self._client: Optional[httpx.AsyncClient] = None

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            params=self.params,
            returns={
                "weather": "string",
                "temp": "string",
                "humidity": "string",
                "wind": "string",
            },
            examples=[{"city": "北京"}, {"city": "上海", "date": "tomorrow"}],
        )

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    async def aclose(self) -> None:
        """Close the shared HTTP client when the tool hub shuts down."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def execute(self, city: str, date: str = "") -> ToolResult:
        try:
            city_name = self._to_lookup_name(city)
            client = await self._get_client()
            response = await client.get(
                f"{self.base_url}/{city_name}",
                params={"format": "j1", "lang": "zh"},
            )
            response.raise_for_status()
            data = response.json()

            current = data.get("current_condition", [{}])[0]
            weather_desc = current.get("weatherDesc", [{}])[0].get("value", "")
            temp = current.get("temp_C", "")
            humidity = current.get("humidity", "")
            wind = current.get("windspeedKmph", "")

            return ToolResult(
                success=True,
                data={
                    "city": city,
                    "date": date or "当前",
                    "weather": weather_desc,
                    "temp": temp,
                    "humidity": humidity,
                    "wind": wind,
                },
                meta={
                    "summary": (
                        f"{city}天气: {weather_desc}, 温度{temp}°C, "
                        f"湿度{humidity}%, 风速{wind}km/h"
                    )
                },
            )
        except httpx.TimeoutException as exc:
            self.logger.error(self.name, f"timeout: {exc}")
            return ToolResult(success=False, error=f"网络超时: {exc}")
        except Exception as exc:
            self.logger.error(self.name, f"request failed: {exc}")
            return ToolResult(success=False, error=str(exc))

    def _to_lookup_name(self, city: str) -> str:
        mapping = {
            "北京": "Beijing",
            "上海": "Shanghai",
            "广州": "Guangzhou",
            "深圳": "Shenzhen",
            "杭州": "Hangzhou",
            "成都": "Chengdu",
            "武汉": "Wuhan",
            "西安": "Xian",
            "重庆": "Chongqing",
            "厦门": "Xiamen",
            "南京": "Nanjing",
            "天津": "Tianjin",
            "苏州": "Suzhou",
            "青岛": "Qingdao",
            "长沙": "Changsha",
            "大连": "Dalian",
            "沈阳": "Shenyang",
            "郑州": "Zhengzhou",
        }
        return mapping.get(city, city)
