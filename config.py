"""配置"""
import os
from dotenv import load_dotenv

load_dotenv()

# LLM 配置
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")

# 抓取配置
FETCH_TIMEOUT = int(os.getenv("FETCH_TIMEOUT", "30"))
FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

# 调试
DEBUG = os.getenv("DEBUG", "false").lower() == "true"
