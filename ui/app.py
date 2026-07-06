"""Web UI"""
import streamlit as st
from openai import OpenAI

from core.agent import Agent
from storage.skill import get_skill_store
from infra.config import config


def main():
    st.set_page_config(page_title="Skill Agent", page_icon="📚", layout="wide")
    
    # 初始化
    if "agent" not in st.session_state:
        client = OpenAI(api_key=config.llm_api_key, base_url=config.llm_base_url)
        st.session_state.agent = Agent(llm_client=client)
        st.session_state.messages = []
    
    st.title("📚 Skill Agent")
    st.markdown("*技能 = 方法论 + 步骤 + 代码（可选）*")
    
    # 侧边栏 - 技能库
    with st.sidebar:
        st.header("📦 技能库")
        store = get_skill_store()
        skills = store.list_all()
        
        if skills:
            for s in skills:
                st.markdown(f"**{s.name}**")
                st.caption(f"标签: {', '.join(s.tags)}")
        else:
            st.info("暂无技能，系统会自动学习")
        
        st.divider()
        
        if st.button("重置对话"):
            st.session_state.agent.reset()
            st.session_state.messages = []
            st.rerun()
    
    # 对话历史
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
    
    # 输入
    if prompt := st.chat_input("输入任务..."):
        # 用户消息
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        
        # 助手响应
        with st.chat_message("assistant"):
            with st.spinner("思考中..."):
                response = st.session_state.agent.chat(prompt)
                st.markdown(response)
        
        st.session_state.messages.append({"role": "assistant", "content": response})


if __name__ == "__main__":
    main()
