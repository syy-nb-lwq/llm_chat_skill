"""天气查询工具"""
import time
from datetime import datetime, timedelta
import requests

from tools.base import Tool, ToolResult, ToolSchema, ToolParam
from infra.logger import get_logger, LogType


class WeatherTool(Tool):
    """天气查询工具"""

    name = "weather_query"
    description = "查询指定城市和日期的天气,返回温度、天气状况、风力、湿度等信息"

    # 中文城市名到英文的映射
    CITY_MAP = {
        "厦门": "Xiamen", "福州": "Fuzhou", "泉州": "Quanzhou", "漳州": "Zhangzhou",
        "北京": "Beijing", "上海": "Shanghai", "广州": "Guangzhou", "深圳": "Shenzhen",
        "杭州": "Hangzhou", "南京": "Nanjing", "成都": "Chengdu", "重庆": "Chongqing",
        "武汉": "Wuhan", "西安": "Xian", "青岛": "Qingdao", "大连": "Dalian",
        "天津": "Tianjin", "苏州": "Suzhou", "长沙": "Changsha", "郑州": "Zhengzhou",
    }

    def __init__(self):
        self.base_url = "https://wttr.in"
        self.logger = get_logger()

    # ---------- 新协议 ----------
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            params=[
                ToolParam(name="city", type="string", description="城市中文名,如'厦门'", required=True),
                ToolParam(name="date", type="string",
                          description="日期,格式 YYYY-MM-DD 或 'today'/'tomorrow'/'今天'/'明天'",
                          required=False, default="today"),
            ],
            returns={
                "city": "string",
                "date": "string",
                "summary": "string",
                "temp_min": "string",
                "temp_max": "string",
                "humidity": "string",
                "wind": "string",
            },
            examples=[{"city": "厦门", "date": "tomorrow"}, {"city": "北京"}],
        )

    def execute(self, city: str, date: str = "today") -> ToolResult:
        start = time.time()
        try:
            target_date = self._parse_date(date) if date else datetime.now().strftime("%Y-%m-%d")
            city_en = self._get_city_name(city)
            url = f"{self.base_url}/{city_en}"
            params = {"format": "j1", "lang": "zh"}
            self.logger.info(LogType.TOOL_CALL, self.name,
                             f"请求天气 API", {"url": url, "city": city_en})
            response = requests.get(url, params=params, timeout=15)
            if response.status_code != 200:
                return ToolResult(
                    success=False,
                    error=f"API 请求失败: {response.status_code}",
                    meta={"duration_ms": (time.time() - start) * 1000},
                )
            data = response.json()
            weather_data = self._parse_weather_data(data, target_date, city)
            return ToolResult(
                success=True,
                data=weather_data,
                meta={"source": "wttr.in", "duration_ms": (time.time() - start) * 1000},
            )
        except requests.exceptions.Timeout:
            return ToolResult(success=False, error="天气查询超时",
                              meta={"duration_ms": (time.time() - start) * 1000})
        except requests.exceptions.RequestException as e:
            return ToolResult(success=False, error=f"网络请求错误: {e}",
                              meta={"duration_ms": (time.time() - start) * 1000})
        except Exception as e:
            return ToolResult(success=False, error=f"未知错误: {e}",
                              meta={"duration_ms": (time.time() - start) * 1000})

    # ---------- 原有解析方法 ----------
    def _get_city_name(self, city: str) -> str:
        if city.lower() not in [c.lower() for c in self.CITY_MAP.keys()]:
            return city
        for cn, en in self.CITY_MAP.items():
            if cn in city or city.lower() == cn.lower():
                return en
        return city

    def _parse_date(self, date_str: str) -> str:
        date_str = date_str.strip()
        if "月" in date_str and "日" in date_str:
            try:
                month = int(date_str.split("月")[0])
                day = int(date_str.split("月")[1].replace("日", ""))
                now = datetime.now()
                year = now.year
                if month < now.month:
                    year += 1
                return f"{year}-{month:02d}-{day:02d}"
            except Exception:
                pass
        s = date_str.lower()
        if s in ["today", "今天"]:
            return datetime.now().strftime("%Y-%m-%d")
        if s in ["tomorrow", "明天"]:
            return (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
            return date_str
        except Exception:
            pass
        try:
            parts = date_str.split("-")
            if len(parts) == 2:
                month, day = int(parts[0]), int(parts[1])
                now = datetime.now()
                year = now.year
                if month < now.month:
                    year += 1
                return f"{year}-{month:02d}-{day:02d}"
        except Exception:
            pass
        return datetime.now().strftime("%Y-%m-%d")

    def _parse_weather_data(self, data: dict, target_date: str, original_city: str) -> dict:
        try:
            nearest_area = data.get("nearest_area", [{}])[0]
            returned_city = nearest_area.get("areaName", [{}])[0].get("value", "")
            city_matched = original_city.lower() in returned_city.lower() or \
                           returned_city.lower() in original_city.lower()
            current = data.get("current_condition", [{}])[0]
            weather_forecast = data.get("weather", [])
            target_idx = 0
            try:
                target_dt = datetime.strptime(target_date, "%Y-%m-%d")
                today = datetime.now()
                diff_days = (target_dt.date() - today.date()).days
                if 0 <= diff_days < len(weather_forecast):
                    target_idx = diff_days
            except Exception:
                pass
            result = {
                "查询城市": original_city,
                "返回城市": returned_city if returned_city else "未知",
                "城市匹配": "✓" if city_matched else "⚠ 可能不准确",
                "查询日期": target_date,
                # 新增简洁结构(给 DAG / Orchestrator 用)
                "city": original_city,
                "date": target_date,
                "summary": current.get("weatherDesc", [{}])[0].get("value", "未知"),
                "temp_now": current.get("temp_C", "N/A"),
                "temp_min": weather_forecast[target_idx].get("mintempC", "N/A") if weather_forecast else "N/A",
                "temp_max": weather_forecast[target_idx].get("maxtempC", "N/A") if weather_forecast else "N/A",
                "humidity": current.get("humidity", "N/A"),
                "wind": f"{current.get('windspeedKmph', 'N/A')} km/h",
                # 保留详细
                "当前天气": {
                    "温度": f"{current.get('temp_C', 'N/A')}°C",
                    "体感温度": f"{current.get('FeelsLikeC', 'N/A')}°C",
                    "天气状况": current.get("weatherDesc", [{}])[0].get("value", "未知"),
                    "风速": f"{current.get('windspeedKmph', 'N/A')} km/h",
                    "湿度": f"{current.get('humidity', 'N/A')}%",
                },
            }
            if weather_forecast and target_idx < len(weather_forecast):
                forecast = weather_forecast[target_idx]
                result["天气预报"] = {
                    "最高温度": f"{forecast.get('maxtempC', 'N/A')}°C",
                    "最低温度": f"{forecast.get('mintempC', 'N/A')}°C",
                    "天气": forecast.get("hourly", [{}])[4].get("weatherDesc", [{}])[0].get("value", "未知"),
                    "降雨概率": f"{forecast.get('hourly', [{}])[4].get('chanceofrain', 'N/A')}%",
                }
            return result
        except (KeyError, IndexError) as e:
            return {"错误": f"数据解析失败: {e}", "原始数据": str(data)[:500]}