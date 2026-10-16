
import json
from google import genai
from google.genai import types
from tools import file_tools
import streamlit as st

st.write("# Query Generation Agent")
client = genai.Client(api_key="")


def gemini_tool(user_prompt):
    print("Gemini Tool Accessed")

    message_history.append({
        "role": "user",
        "parts": [{"text": user_prompt}]
    })

    response = client.models.generate_content(
        model="gemini-flash-lite-latest",  # keep your existing model
        contents=message_history,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            
        ),
    )

    print("--------------------------------")
    print(response.usage_metadata)
    print("--------------------------------")

    message_history.append({
        "role": "model",
        "parts": [{"text": response.text}]
    })

    return response.text



# Chat session : Context is maintained in the message_history list
message_history = []
# System prompt is the initial prompt that is used to set the context of the chat session
system_prompt = f"""
    Act as a Database query expert
    You are tasked to generate a required sql query / streamlit code for UI for the asked user question.
    please refer schema.sql file to understand the database schema.
    You are given a free hand to think and analyse to arrive at a answer. 
    You can ask as many questions as you want and make sure you are not asking more than 3 questions. 
    and you can also use as many tools as you want to acheive the answer. 
    you can use streamlit code to generate the UI for the answer.
    generate large code if needed.

    "clarifiation" If user question is vague or not clear, use reprompt to ask for clarification.
    "clarification" Do not ask too many questions at a time.

    "function-tools" : You can use the following tool or tools and combine the results to acheive the answer.
     Available tools:
     - create_file : create_file(path: str, content: str) -> str Create a new file with the given content.
     - update_file : update_file(path: str, content: str) -> str: Update the content of the file with the given content.
     - read_file : read_file(path: str) -> str Append the content to the file.
     - append_file : append_file(path: str, content: str) -> str
     - replace_in_file(path: str, old: str, new: str) -> str:

    If data is required, return:
    {{
        "data_needed": true,
        "data": {{"tool": "read_file", "arguments": {{"path": "schema.sql"}}}},
        "reprompt": false
    }}
    Note following contraints to be followed strictly when data is requred :
    1. you can use one tool at a time.  but also check context and again you can request for new data if needed
    2. you please use reprompt to ask for clarification if needed.
    3. you can use the tools only if data is required.

    If data is not required, return:
    {{
        "data_needed": false,
        "data": null,
        "reprompt": false,
        "response": "response to the user question"
    }}

    1. use reprompt to ask for clarification if needed. when data is not required / clear.
    and include a response asking for clarification.

    When you are finally clear with the data and clarifications generate the answer and return the response in the following format:
    {{
        "data_needed": false,
        "data": null,
        "reprompt": false,
        "response": "response to the user question"
    }}

    if any calculations are needed please use the standard forumulas as per the standards. 
"""


TOOLS = {
    "create_file": file_tools.create_file,
    "update_file": file_tools.update_file,
    "read_file": file_tools.read_file,
    "append_file": file_tools.append_file,
    "replace_in_file": file_tools.replace_in_file,
}


def call_tool(data):
    if isinstance(data, dict):
        tool_name = data.get("tool")
        arguments = data.get("arguments") or {}
    else:
        tool_name = data
        arguments = {}

    if tool_name not in TOOLS or not isinstance(arguments, dict):
        raise KeyError(tool_name)
    return TOOLS[tool_name](**arguments)


while True:

    user_question = input("Enter your query: ")

    if user_question.lower() in ["exit", "quit"]:
        break

    user_prompt = f"User question: {user_question}. Keep the answer short and concise."

    try:
        for _ in range(6):
            response_json = json.loads(gemini_tool(user_prompt))
            print(response_json)

            if response_json["reprompt"]:
                print(response_json.get("response", "Please clarify your question."))
                break

            if not response_json["data_needed"]:
                print(json.dumps(response_json, indent=2))
                break

            try:
                snapshot = call_tool(response_json["data"])
            except KeyError:
                print("Invalid data")
                break
            except TypeError as error:
                print(f"Invalid arguments: {error}")
                break

            user_prompt = (
                f"User question: {user_question}\n"
                f"Required data: {json.dumps(snapshot)}\n"
                "If another tool is required, return data_needed=true and the next tool. "
                "Otherwise answer concisely in valid JSON "
                "with data_needed=false, data=null, reprompt=false, "
                "and response containing the answer."
            )
        else:
            print("Too many tool calls.")

    except (json.JSONDecodeError, KeyError) as error:
        print(f"Invalid response: {error}")