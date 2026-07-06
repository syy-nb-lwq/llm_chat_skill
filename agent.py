"""Tool Calling Agent - 带记忆和学习能力的智能体，流式输出版"""
import json
import os
import re
from typing import Optional, List, Dict, Callable
from openai import OpenAI

from config import LLM_BASE_URL, LLM_API_KEY, LLM_MODEL
from tools.registry import registry
from tools.memory import get_memory


def msg_to_dict(msg) -> dict:
    """将 ChatCompletionMessage 转换为字典"""
    result = {
        "role": msg.role,
        "content": msg.content or ""
    }
    if hasattr(msg, 'tool_calls') and msg.tool_calls:
        result["tool_calls"] = []
        for tc in msg.tool_calls:
            result["tool_calls"].append({
                "id": tc.id,
                "type": tc.type,
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments
                }
            })
    return result


class KnowledgeBase:
    """知识库 - 存储已读取的文件内容"""
    
    def __init__(self):
        self.entries: Dict[str, Dict] = {}
    
    def add(self, file_id: str, content: str, metadata: dict):
        """添加知识条目"""
        self.entries[file_id] = {
            "content": content,
            "metadata": metadata,
            "summary": content[:200] + "..." if len(content) > 200 else content
        }
    
    def get(self, file_id: str) -> Optional[str]:
        """获取内容"""
        entry = self.entries.get(file_id)
        return entry["content"] if entry else None
    
    def search(self) -> str:
        """获取所有知识"""
        if not self.entries:
            return "知识库为空"
        
        results = ["知识库中的内容:\n"]
        for fid, entry in self.entries.items():
            content = entry["content"]
            content_preview = content[:1000] + "..." if len(content) > 1000 else content
            results.append("\n【" + fid + "】")
            results.append(content_preview)
        
        return "\n".join(results)


class StreamingAgent:
    """流式输出 Agent"""
    
    MAX_ITERATIONS = 10
    
    SYSTEM_PROMPT = """你是一个智能助手，有多个工具可以使用。

可用工具：
{tools}

特殊能力：
1. 你有一个知识库，存储了之前读取的文件内容
2. 你有用户记忆系统，可以记住用户偏好
3. 当用户询问与之前文件相关的问题时，可以基于知识库回答

使用规则：
1. 如果需要获取外部信息，先调用工具
2. 如果用户询问之前文件的内容，知识库中已有，可以直接回答
3. 如果工具返回不足，可以继续调用其他工具
4. 所有需要的信息都获取后，给出完整回答
5. 根据用户偏好调整回答风格
{user_profile}
6. 回答要简洁、准确"""
    
    def __init__(self, model: str = None, api_key: str = None, base_url: str = None, user_id: str = "default"):
        self.client = OpenAI(
            api_key=api_key or LLM_API_KEY,
            base_url=base_url or LLM_BASE_URL
        )
        self.model = model or LLM_MODEL
        self.messages: List[Dict] = []
        self.knowledge_base = KnowledgeBase()
        self.memory = get_memory(user_id)
        self.user_id = user_id
        self._init_system_prompt()
    
    def _init_system_prompt(self):
        """初始化系统提示"""
        tools_text = self._build_tools_description()
        user_profile = self._get_user_profile()
        system_prompt = self.SYSTEM_PROMPT.format(tools=tools_text, user_profile=user_profile)
        self.messages = [{"role": "system", "content": system_prompt}]
    
    def _get_user_profile(self) -> str:
        """获取用户画像"""
        profile = self.memory.profile
        prefs = profile.get("preferences", {})
        
        top_tools = sorted(profile.get("tool_usage", {}).items(), key=lambda x: x[1], reverse=True)[:3]
        top_files = sorted(profile.get("file_types", {}).items(), key=lambda x: x[1], reverse=True)[:3]
        patterns = profile.get("patterns", [])
        
        profile_text = f"""
用户画像：
- 回答风格偏好：{prefs.get('answer_style', 'normal')}
- 语言偏好：{prefs.get('language', '中文')}
"""
        if top_tools:
            profile_text += f"- 常用工具：{', '.join([t[0] for t in top_tools])}\n"
        if top_files:
            profile_text += f"- 常用文件类型：{', '.join([f[0] for f in top_files])}\n"
        if patterns:
            profile_text += f"- 学习到的模式：{', '.join(patterns[:3])}\n"
        
        return profile_text
    
    def _learn_from_interaction(self, user_input: str, messages: List[Dict]):
        """从交互中学习用户行为"""
        file_extensions = re.findall(r'\.(\w+)', user_input)
        for ext in file_extensions:
            if ext.lower() in ['pdf', 'txt', 'md', 'py', 'js', 'jpg', 'png']:
                self.memory.learn("file_type", {"extension": ext})
        
        for msg in messages:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    tool_name = tc.get("function", {}).get("name", "")
                    if tool_name in ["fetch_webpage", "read_file", "read_pdf", "read_image"]:
                        self.memory.learn("tool_use", {"tool_name": tool_name})
        
        topics = []
        if "pdf" in user_input.lower():
            topics.append("PDF处理")
        if "网页" in user_input or "网站" in user_input:
            topics.append("网页抓取")
        if "图片" in user_input or "截图" in user_input:
            topics.append("图片处理")
        for topic in topics:
            self.memory.learn("topic", {"topic": topic})
        
        self.memory.learn("context", {"context": user_input[:100]})
    
    def reset(self):
        """重置对话和知识库"""
        self._init_system_prompt()
        self.knowledge_base = KnowledgeBase()
    
    def chat_streaming(self, user_input: str, callback: Callable = None) -> str:
        """流式对话"""
        def send(msg: str):
            if callback:
                callback(msg)
        
        # 如果知识库有内容，在用户消息中加入知识库信息
        if self.knowledge_base.entries:
            knowledge_info = "\n\n[知识库内容]\n" + self.knowledge_base.search() + "\n[/知识库内容]"
            user_input_with_knowledge = user_input + knowledge_info
        else:
            user_input_with_knowledge = user_input
        
        self.messages.append({"role": "user", "content": user_input_with_knowledge})
        
        iteration = 0
        all_messages = []
        full_response = []
        
        while iteration < self.MAX_ITERATIONS:
            iteration += 1
            
            # 发送思考开始信号
            send("\n💭 思考中...\n")
            
            response = self.client.chat.completions.create(
                model=self.model,
                messages=self.messages,
                tools=registry.get_openai_format(),
                tool_choice="auto"
            )
            
            msg = response.choices[0].message
            msg_dict = msg_to_dict(msg)
            self.messages.append(msg_dict)
            all_messages.append(msg_dict)
            
            if not msg.tool_calls:
                # 对话结束，学习用户行为
                self._learn_from_interaction(user_input, all_messages)
                
                answer = msg.content or ""
                send("\n📝 回答:\n")
                send(answer)
                return answer
            
            # 执行工具调用
            for call in msg.tool_calls:
                tool_name = call.function.name
                args = json.loads(call.function.arguments)
                
                send(f"\n🔧 调用工具: {tool_name}\n")
                
                try:
                    # 特殊处理：将文件内容存入知识库
                    if tool_name in ["read_file", "read_pdf", "read_image", "fetch_webpage"]:
                        if tool_name == "fetch_webpage":
                            result = registry.execute(tool_name, args)
                            file_id = args.get("url", "unknown")
                        else:
                            result = registry.execute(tool_name, args)
                            file_id = args.get("file_path", "unknown")
                        
                        error_prefixes = ["文件不存在", "不支持", "获取失败", "缺少依赖", "读取失败", "OCR 失败", "403"]
                        is_error = any(result.startswith(prefix) for prefix in error_prefixes)
                        
                        if not is_error:
                            self.knowledge_base.add(
                                file_id=file_id,
                                content=result,
                                metadata={"source": tool_name, "args": str(args)}
                            )
                    else:
                        result = registry.execute(tool_name, args)
                except Exception as e:
                    result = "工具执行失败: " + str(e)
                
                # 显示工具结果
                result_preview = result[:200] + "..." if len(result) > 200 else result
                send(f"📋 结果: {result_preview}\n")
                
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": str(result)[:8000]
                })
        
        return "达到最大迭代次数，停止执行"
    
    def _build_tools_description(self) -> str:
        """构建工具描述"""
        lines = []
        for tool in registry.list_all():
            lines.append(f"\n## {tool.name}")
            lines.append(f"描述: {tool.description}")
            if tool.parameters.get("properties"):
                lines.append("参数:")
                for param_name, param_info in tool.parameters["properties"].items():
                    param_type = param_info.get("type", "any")
                    param_desc = param_info.get("description", "")
                    lines.append(f"  - {param_name} ({param_type}): {param_desc}")
        return "\n".join(lines)
    
    def get_memory_profile(self) -> str:
        """获取用户画像摘要"""
        return self.memory.get_summary()
    
    def clear_memory(self):
        """清除记忆"""
        self.memory.clear()
    
    # 兼容旧接口
    def chat(self, user_input: str) -> str:
        """多轮对话（非流式）"""
        return self._chat(user_input)
    
    def _chat(self, user_input: str) -> str:
        """核心对话逻辑"""
        if self.knowledge_base.entries:
            knowledge_info = "\n\n[知识库内容]\n" + self.knowledge_base.search() + "\n[/知识库内容]"
            user_input_with_knowledge = user_input + knowledge_info
        else:
            user_input_with_knowledge = user_input
        
        self.messages.append({"role": "user", "content": user_input_with_knowledge})
        
        iteration = 0
        all_messages = []
        
        while iteration < self.MAX_ITERATIONS:
            iteration += 1
            
            response = self.client.chat.completions.create(
                model=self.model,
                messages=self.messages,
                tools=registry.get_openai_format(),
                tool_choice="auto"
            )
            
            msg = response.choices[0].message
            msg_dict = msg_to_dict(msg)
            self.messages.append(msg_dict)
            all_messages.append(msg_dict)
            
            if not msg.tool_calls:
                self._learn_from_interaction(user_input, all_messages)
                return msg.content
            
            for call in msg.tool_calls:
                tool_name = call.function.name
                args = json.loads(call.function.arguments)
                
                try:
                    if tool_name in ["read_file", "read_pdf", "read_image", "fetch_webpage"]:
                        if tool_name == "fetch_webpage":
                            result = registry.execute(tool_name, args)
                            file_id = args.get("url", "unknown")
                        else:
                            result = registry.execute(tool_name, args)
                            file_id = args.get("file_path", "unknown")
                        
                        error_prefixes = ["文件不存在", "不支持", "获取失败", "缺少依赖", "读取失败", "OCR 失败", "403"]
                        is_error = any(result.startswith(prefix) for prefix in error_prefixes)
                        
                        if not is_error:
                            self.knowledge_base.add(
                                file_id=file_id,
                                content=result,
                                metadata={"source": tool_name, "args": str(args)}
                            )
                    else:
                        result = registry.execute(tool_name, args)
                except Exception as e:
                    result = "工具执行失败: " + str(e)
                
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": str(result)[:8000]
                })
        
        return "达到最大迭代次数，停止执行"


# 兼容旧名称
ToolCallingAgent = StreamingAgent


# ============ 对话式界面 ============

def run_cli():
    """命令行对话界面"""
    print("=" * 60)
    print("Tool Calling Agent - 带记忆和学习版 (流式输出)")
    print("=" * 60)
    print("输入 'reset' 重置对话，'knowledge' 查看知识库，'profile' 查看用户画像，'quit' 退出")
    print()
    
    agent = StreamingAgent()
    
    def stream_callback(text: str):
        print(text, end="")
    
    while True:
        try:
            user_input = input("你: ").strip()
            
            if not user_input:
                continue
            
            if user_input.lower() in ['quit', 'exit', 'q']:
                print("再见!")
                break
            
            if user_input.lower() == 'reset':
                agent.reset()
                print("对话已重置，知识库已清空\n")
                continue
            
            if user_input.lower() == 'knowledge':
                print("\n知识库内容:")
                print(agent.knowledge_base.search())
                print()
                continue
            
            if user_input.lower() == 'profile':
                print("\n" + agent.get_memory_profile())
                print()
                continue
            
            print()
            agent.chat_streaming(user_input, callback=stream_callback)
            print("\n")
            
        except KeyboardInterrupt:
            print("\n再见!")
            break
        except Exception as e:
            print("错误: " + str(e) + "\n")


# ============ 便捷函数 ============

def chat(user_input: str, user_id: str = "default") -> str:
    """便捷对话函数"""
    agent = StreamingAgent(user_id=user_id)
    return agent.chat(user_input)


if __name__ == "__main__":
    run_cli()
