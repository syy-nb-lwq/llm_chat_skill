# 技能：get_weather

## 元信息
- ID: skill_get_weather_f48b0714
- 版本: 1.0
- 创建时间: 2026-07-06T17:24:24.195325

## 描述
根据城市名称查询当前天气和未来几天的天气预报。支持中文和英文城市名，使用 wttr.in 免费 API，返回温度、湿度、风速、天气状况等友好可读信息。

## 参数说明
```json
{
  "type": "object",
  "properties": {
    "city": {
      "type": "string",
      "description": "城市名称，支持中文或英文"
    },
    "days": {
      "type": "integer",
      "description": "预报天数（1-7），默认3"
    },
    "unit": {
      "type": "string",
      "description": "温度单位，可选 celsius(摄氏度) 或 fahrenheit(华氏度)"
    }
  }
}
```

## 函数代码
```python
def get_weather(city: str, days: int = 3, unit: str = "celsius") -> str:
    """
    根据城市名称查询天气信息。

    :param city: 城市名称，支持中文或英文
    :param days: 预报天数（1-7），默认3；设为0则只返回当前天气
    :param unit: 温度单位，celsius（摄氏度）或 fahrenheit（华氏度）
    :return: 格式化的天气信息字符串
    """
    import requests
    import json
    from urllib.parse import quote

    try:
        # 限制天数范围
        if days < 0 or days > 7:
            days = 3

        # URL 编码城市名
        encoded_city = quote(city)
        
        # 根据单位选择API参数
        if unit == "fahrenheit":
            url = f"https://wttr.in/{encoded_city}?format=j1&u"
        else:
            url = f"https://wttr.in/{encoded_city}?format=j1&m"

        # 发送请求
        response = requests.get(url, timeout=10)
        response.raise_for_status()

        data = response.json()

        # 检查返回数据是否有效
        if "error" in data or "current_condition" not in data:
            return f"抱歉，未找到城市「{city}」的天气信息，请检查城市名称是否正确。"

        # 提取当前天气
        current = data["current_condition"][0]
        
        if unit == "fahrenheit":
            temp = current["temp_F"]
            feels_like = current["FeelsLikeF"]
            wind_speed = current["windspeedMiles"]
            temp_unit = "°F"
            speed_unit = "mph"
        else:
            temp = current["temp_C"]
            feels_like = current["FeelsLikeC"]
            wind_speed = current["windspeedKmph"]
            temp_unit = "°C"
            speed_unit = "km/h"

        humidity = current["humidity"]
        weather_desc = current["weatherDesc"][0]["value"]
        visibility = current.get("visibility", "N/A")

        # 构建当前天气信息
        result_parts = [
            f"📅 当前城市：{city}",
            f"🌤 天气状况：{weather_desc}",
            f"🌡 温度：{temp}{temp_unit}（体感 {feels_like}{temp_unit}）",
            f"💧 湿度：{humidity}%",
            f"🌬 风速：{wind_speed} {speed_unit}",
            f"👁 能见度：{visibility} km"
        ]

        # 如果有预报需求
        if days > 0:
            weather_forecast = data.get("weather", [])
            forecast_to_show = min(days, len(weather_forecast))
            if forecast_to_show > 0:
                result_parts.append("\n📆 未来天气预报：")
                for i in range(forecast_to_show):
                    day = weather_forecast[i]
                    date = day["date"]
                    if unit == "fahrenheit":
                        max_temp = day["maxtempF"]
                        min_temp = day["mintempF"]
                    else:
                        max_temp = day["maxtempC"]
                        min_temp = day["mintempC"]
                    
                    hourly = day.get("hourly", [])
                    if hourly:
                        mid_index = min(4, len(hourly)-1)
                        day_desc = hourly[mid_index]["weatherDesc"][0]["value"]
                    else:
                        day_desc = "未知"
                    result_parts.append(f"   {date}: {day_desc}, 最高 {max_temp}{temp_unit} / 最低 {min_temp}{temp_unit}")

        return "\n".join(result_parts)

    except requests.exceptions.RequestException as e:
        return f"🌐 网络请求失败：{e}，请检查网络连接。"
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        return f"⚠️ 数据解析错误：{e}，可能是城市名无效或返回格式异常。"
```

## 使用示例
无
