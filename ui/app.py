"""Web UI"""
import streamlit as st

from core.agent import Agent
from skills.manager import get_skill_store


def main():
    st.set_page_config(page_title="Skill Agent", page_icon="📚")
    
    # 初始化
    if "agent" not in st.session_state:
        st.session_state.agent = Agent()
        st.session_state.messages = []
    
    st.title("📚 Skill Agent")
    st.markdown("*技能 = 方法论 + 步骤 + 代码（可选）*")
    
    # 侧边栏
    with st.sidebar:
        st.header("📦 技能库")
        store = get_skill_store()
        skills = store.list_all()
        
        if skills:
            for s in skills:
                st.markdown(f"**{s.name}**")
                st.caption(f"标签: {', '.join(s.tags)}")
        else:
            st.info("暂无技能")
        
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
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        
        with st.chat_message("assistant"):
            with st.spinner("思考中..."):
                response = st.session_state.agent.chat(prompt)
                st.markdown(response)
        
        st.session_state.messages.append({"role": "assistant", "content": response})


if __name__ == "__main__":
    main()
