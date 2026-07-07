"""天气查询工具"""
import requests
from datetime import datetime, timedelta
from tools.base import Tool, ToolResult
from infra.logger import get_logger, LogType


class WeatherTool(Tool):
    """天气查询工具"""
    
    name = "weather_query"
    description = "查询指定城市和日期的天气情况，返回温度、天气状况、风力等信息"
    
    # 中文城市名到英文的映射
    CITY_MAP = {
        "厦门": "Xiamen",
        "福州": "Fuzhou", 
        "泉州": "Quanzhou",
        "漳州": "Zhangzhou",
        "北京": "Beijing",
        "上海": "Shanghai",
        "广州": "Guangzhou",
        "深圳": "Shenzhen",
        "杭州": "Hangzhou",
        "南京": "Nanjing",
        "成都": "Chengdu",
        "重庆": "Chongqing",
        "武汉": "Wuhan",
        "西安": "Xian",
        "青岛": "Qingdao",
        "大连": "Dalian",
        "天津": "Tianjin",
        "苏州": "Suzhou",
        "长沙": "Changsha",
        "郑州": "Zhengzhou",
    }
    
    def __init__(self):
        self.base_url = "https://wttr.in"
        self.logger = get_logger()
    
    def execute(self, city: str, date: str = None) -> ToolResult:
        """
        查询天气
        
        Args:
            city: 城市名称，如"厦门"、"Xiamen"
            date: 日期，格式 YYYY-MM-DD 或 "today" / "tomorrow"
        """
        # 记录输入数据
        self.logger.log_data(self.name, "in", "city", city)
        if date:
            self.logger.log_data(self.name, "in", "date", date)
        
        try:
            # 解析日期
            target_date = self._parse_date(date) if date else datetime.now().strftime("%Y-%m-%d")
            
            # 转换为英文城市名
            city_en = self._get_city_name(city)
            
            # 记录数据转换
            self.logger.log_data(self.name, "transform", "city_en", city_en)
            self.logger.log_data(self.name, "transform", "target_date", target_date)
            
            # 调用天气 API
            url = f"{self.base_url}/{city_en}"
            params = {
                "format": "j1",
                "lang": "zh"
            }
            
            self.logger.info(LogType.TOOL_CALL, self.name, f"请求天气 API", {"url": url, "city": city_en})
            
            response = requests.get(url, params=params, timeout=15)
            
            if response.status_code != 200:
                error_msg = f"API 请求失败: {response.status_code}"
                self.logger.error(LogType.TOOL_ERROR, self.name, error_msg)
                return ToolResult(success=False, error=error_msg)
            
            data = response.json()
            weather_data = self._parse_weather_data(data, target_date, city)
            
            # 记录输出数据
            self.logger.log_data(self.name, "out", "weather_data", weather_data)
            self.logger.info(LogType.TOOL_SUCCESS, self.name, f"天气查询成功", {"city": city, "date": target_date})
            
            return ToolResult(success=True, data=weather_data)
            
        except requests.exceptions.Timeout:
            error_msg = "天气查询超时，请稍后重试"
            self.logger.error(LogType.TOOL_ERROR, self.name, error_msg)
            return ToolResult(success=False, error=error_msg)
        except requests.exceptions.RequestException as e:
            error_msg = f"网络请求错误: {str(e)}"
            self.logger.error(LogType.TOOL_ERROR, self.name, error_msg)
            return ToolResult(success=False, error=error_msg)
        except Exception as e:
            error_msg = f"未知错误: {str(e)}"
            self.logger.error(LogType.TOOL_ERROR, self.name, error_msg)
            return ToolResult(success=False, error=error_msg)
    
    def _get_city_name(self, city: str) -> str:
        """获取英文城市名"""
        # 如果已经是英文，直接返回
        if city.lower() not in [c.lower() for c in self.CITY_MAP.keys()]:
            return city
        
        for cn, en in self.CITY_MAP.items():
            if cn in city or city.lower() == cn.lower():
                return en
        return city
    
    def _parse_date(self, date_str: str) -> str:
        """解析日期字符串"""
        date_str = date_str.strip()
        
        # 处理中文日期
        if "月" in date_str and "日" in date_str:
            # 解析 "7月8日" 格式
            try:
                month = int(date_str.split("月")[0])
                day = int(date_str.split("月")[1].replace("日", ""))
                now = datetime.now()
                year = now.year
                # 如果解析的月份小于当前月份，说明是明年
                if month < now.month:
                    year += 1
                return f"{year}-{month:02d}-{day:02d}"
            except:
                pass
        
        # 处理相对日期
        if date_str.lower() in ["today", "今天"]:
            return datetime.now().strftime("%Y-%m-%d")
        elif date_str.lower() in ["tomorrow", "明天"]:
            return (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        
        # 尝试直接解析 YYYY-MM-DD
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
            return date_str
        except:
            pass
        
        # 尝试解析 MM-DD
        try:
            parts = date_str.split("-")
            if len(parts) == 2:
                month, day = int(parts[0]), int(parts[1])
                now = datetime.now()
                year = now.year
                if month < now.month:
                    year += 1
                return f"{year}-{month:02d}-{day:02d}"
        except:
            pass
        
        return datetime.now().strftime("%Y-%m-%d")
    
    def _parse_weather_data(self, data: dict, target_date: str, original_city: str) -> dict:
        """解析天气数据"""
        try:
            # 检查是否返回了正确的城市
            nearest_area = data.get("nearest_area", [{}])[0]
            returned_city = nearest_area.get("areaName", [{}])[0].get("value", "")
            
            # 如果城市不匹配，记录警告但继续处理
            city_matched = original_city.lower() in returned_city.lower() or \
                          returned_city.lower() in original_city.lower()
            
            # 获取当前天气
            current = data.get("current_condition", [{}])[0]
            
            # 获取天气预报
            weather_forecast = data.get("weather", [])
            
            # 计算目标日期对应的预报索引
            target_idx = 0
            try:
                target_dt = datetime.strptime(target_date, "%Y-%m-%d")
                today = datetime.now()
                diff_days = (target_dt.date() - today.date()).days
                if 0 <= diff_days < len(weather_forecast):
                    target_idx = diff_days
            except:
                pass
            
            # 格式化输出
            result = {
                "查询城市": original_city,
                "返回城市": returned_city if returned_city else "未知",
                "城市匹配": "✓" if city_matched else "⚠ 可能不准确",
                "查询日期": target_date,
                "当前天气": {
                    "温度": f"{current.get('temp_C', 'N/A')}°C",
                    "体感温度": f"{current.get('FeelsLikeC', 'N/A')}°C",
                    "天气状况": current.get('weatherDesc', [{}])[0].get('value', '未知'),
                    "风速": f"{current.get('windspeedKmph', 'N/A')} km/h",
                    "湿度": f"{current.get('humidity', 'N/A')}%",
                }
            }
            
            # 添加天气预报
            if weather_forecast and target_idx < len(weather_forecast):
                forecast = weather_forecast[target_idx]
                result["天气预报"] = {
                    "最高温度": f"{forecast.get('maxtempC', 'N/A')}°C",
                    "最低温度": f"{forecast.get('mintempC', 'N/A')}°C",
                    "天气": forecast.get('hourly', [{}])[4].get('weatherDesc', [{}])[0].get('value', '未知'),
                    "降雨概率": f"{forecast.get('hourly', [{}])[4].get('chanceofrain', 'N/A')}%",
                }
            
            return result
            
        except (KeyError, IndexError) as e:
            return {"错误": f"数据解析失败: {str(e)}", "原始数据": str(data)[:500]}
