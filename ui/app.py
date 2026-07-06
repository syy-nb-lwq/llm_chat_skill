"""Web UI"""
import streamlit as st
from openai import OpenAI

from core.agent import Agent
from core.plugin import PluginRegistry
from plugins.web import WebPlugin
from plugins.file import FilePlugin
from plugins.code import CodePlugin
from storage.skill import get_skill_store
from infra.config import config


def init_agent():
    """初始化 Agent"""
    if "agent" not in st.session_state:
        # 初始化插件
        registry = PluginRegistry()
        registry.register(WebPlugin())
        registry.register(FilePlugin())
        registry.register(CodePlugin())
        
        # 初始化 LLM 客户端
        client = OpenAI(
            api_key=config.llm_api_key,
            base_url=config.llm_base_url
        )
        
        # 初始化 Agent
        st.session_state.agent = Agent(
            llm_client=client,
            registry=registry,
            system_prompt="""你是一个智能助手，有多种工具可以使用。
            
工具：
- fetch_webpage: 抓取网页
- read_file: 读取本地文件

当需要获取外部信息时，先调用工具。
回答要简洁、准确。"""
        )
        
        # 初始化消息历史
        st.session_state.messages = []


def main():
    st.set_page_config(page_title="AI Agent", page_icon="🤖", layout="wide")
    
    init_agent()
    
    st.title("🤖 AI Agent")
    
    # 侧边栏
    with st.sidebar:
        st.header("设置")
        
        if st.button("重置对话"):
            st.session_state.agent.reset()
            st.session_state.messages = []
            st.rerun()
        
        # 技能列表
        st.divider()
        st.subheader("📦 技能库")
        store = get_skill_store()
        skills = store.list_valid()
        if skills:
            for s in skills:
                st.text(f"• {s.name}")
        else:
            st.text("暂无技能")
    
    # 对话历史
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
    
    # 输入
    user_input = st.chat_input("输入你的问题...")
    
    if user_input:
        # 添加用户消息
        st.session_state.messages.append({"role": "user", "content": user_input})
        with st.chat_message("user"):
            st.markdown(user_input)
        
        # 调用 Agent
        with st.chat_message("assistant"):
            with st.spinner("思考中..."):
                response = st.session_state.agent.chat(user_input)
                st.markdown(response)
        
        # 保存助手消息
        st.session_state.messages.append({"role": "assistant", "content": response})


if __name__ == "__main__":
    main()
