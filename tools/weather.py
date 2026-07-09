"""Weather Query Tool - 使用 httpx 异步请求,timeout 真正生效,不阻塞事件循环"""
import httpx
from tools.base import Tool, ToolResult, ToolSchema, ToolParam
from infra.logger import get_logger


class WeatherTool(Tool):
    """天气查询工具"""

    name = "weather_query"
    description = "查询指定城市的天气信息"

    params = [
        ToolParam(name="city", type="string", required=True, description="城市名称(支持中英文)"),
        ToolParam(name="date", type="string", required=False, description="日期(可选,留空查当前)"),
    ]

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            params=self.params,
            returns={"weather": "string", "temp": "string", "humidity": "string", "wind": "string"},
            examples=[{"city": "北京"}, {"city": "上海", "date": "tomorrow"}],
        )

    def __init__(self):
        self.logger = get_logger()
        self.base_url = "https://wttr.in"
        # 共享 AsyncClient(连接池复用); 15s 真正生效,不再被事件循环吞掉
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    async def aclose(self) -> None:
        """工具销毁时关闭 client(挂在 session.dispose_callbacks 上)"""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def execute(self, city: str, date: str = "") -> ToolResult:
        try:
            city_en = self._to_pinyin(city)
            url = f"{self.base_url}/{city_en}"
            params = {"format": "j1", "lang": "zh"}
            self.logger.info(self.name, f"请求天气: {city}")
            client = await self._get_client()
            response = await client.get(url, params=params)
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
                meta={"summary": f"{city}天气: {weather_desc}, 温度{temp}°C, 湿度{humidity}%, 风速{wind}km/h"},
            )
        except httpx.TimeoutException as e:
            self.logger.error(self.name, f"请求超时: {e}")
            return ToolResult(success=False, error=f"网络超时: {e}")
        except Exception as e:
            self.logger.error(self.name, f"请求失败: {e}")
            return ToolResult(success=False, error=str(e))

    def _to_pinyin(self, city: str) -> str:
        mapping = {
            "北京": "Beijing", "上海": "Shanghai", "广州": "Guangzhou",
            "深圳": "Shenzhen", "杭州": "Hangzhou", "成都": "Chengdu",
            "武汉": "Wuhan", "西安": "Xian", "重庆": "Chongqing",
            "厦门": "Xiamen", "南京": "Nanjing", "天津": "Tianjin",
            "苏州": "Suzhou", "青岛": "Qingdao", "长沙": "Changsha",
            "大连": "Dalian", "沈阳": "Shenyang", "郑州": "Zhengzhou",
        }
        return mapping.get(city, city)
