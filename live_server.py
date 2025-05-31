#!/usr/bin/env python3
"""
live_server.py

Run a smolagents CodeAgent with a live web UI showing the knowledge graph and timeline in real time.
Start this script, then open http://localhost:8000 in your browser.
"""
import os
import time
import json
import queue
import threading
from dotenv import load_dotenv
from flask import Flask, Response, send_from_directory
from smolagents import CodeAgent, tool, LiteLLMModel
import requests
import re
from requests.exceptions import RequestException

# Thread-safe queue to hold provenance events
message_queue = queue.Queue()

app = Flask(__name__, static_folder='static')

@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')

@app.route('/events')
def events():
    def event_stream():
        while True:
            event = message_queue.get()
            yield f"data: {json.dumps(event)}\n\n"
    return Response(event_stream(), mimetype="text/event-stream")

def start_server():
    # Requires Flask installed (`pip install flask`)
    app.run(host='0.0.0.0', port=8000, threaded=True)

@tool
def visit_webpage(url: str) -> str:
    """Visits a webpage and returns its Markdown content.
    
    Args:
        url (str): The URL of the webpage to visit.
    """
    try:
        response = requests.get(url)
        response.raise_for_status()
        from markdownify import markdownify
        markdown_content = markdownify(response.text).strip()
        markdown_content = re.sub(r"\n{3,}", "\n\n", markdown_content)
        return markdown_content
    except RequestException as e:
        return f"Error fetching the webpage: {e}"
    except Exception as e:
        return f"Unexpected error: {e}"

def main():
    load_dotenv()

    # Start the server
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()
    time.sleep(1)
    print("Live server running at http://localhost:8000")

    # Initialize the agent
    model_id = os.getenv("SMOL_MODEL_ID", "anthropic/claude-3-7-sonnet-latest")
    model = LiteLLMModel(model_id=model_id)
    tools = [visit_webpage]

    agent = CodeAgent(
        tools=tools,
        model=model,
        add_base_tools=True,
        additional_authorized_imports=["time", "json", "pydoc", "watchdog"],
        max_steps=100
    )

    # Import step types for recording
    from smolagents.memory import TaskStep, PlanningStep, ActionStep, SystemPromptStep, FinalAnswerStep

    def record_step(step):
        agent_name = getattr(agent, 'name', None) or getattr(agent, 'agent_name', type(agent).__name__)
        seq = len(agent.memory.steps) - 1
        rec = {'agent': agent_name, 'sequence': seq}
        if isinstance(step, SystemPromptStep):
            rec.update({'type': 'system_prompt', 'system_prompt': step.system_prompt})
        elif isinstance(step, TaskStep):
            rec.update({'type': 'task', 'task': step.task})
        elif isinstance(step, PlanningStep):
            rec.update({'type': 'planning', 'plan': step.plan})
        elif isinstance(step, ActionStep):
            calls = [tc.dict() for tc in (step.tool_calls or [])]
            rec.update({
                'type': 'action',
                'model_output': step.model_output,
                'tool_calls': calls,
                'observations': step.observations,
                'action_output': step.action_output,
                'start_time': step.start_time,
                'end_time': step.end_time,
                'duration': step.duration,
            })
        elif isinstance(step, FinalAnswerStep):
            rec.update({'type': 'final_answer', 'final_answer': step.final_answer})
        else:
            rec.update({'type': 'unknown'})
        message_queue.put(rec)

    # Interactive REPL
    while True:
        task = input("\nEnter task (or 'exit' to quit): ")
        if task.lower() in ['exit', 'quit']:
            print("Shutting down.")
            break
        message_queue.put({'type': 'task_start', 'task': task, 'timestamp': time.time()})
        try:
            stream = agent.run(task, stream=True)
            if agent.memory.steps:
                record_step(agent.memory.steps[0])
            for step in stream:
                record_step(step)
        except Exception as e:
            message_queue.put({'type': 'error', 'error': str(e)})
        finally:
            message_queue.put({'type': 'task_end', 'task': task, 'timestamp': time.time()})

    message_queue.put({'type': 'session_end', 'timestamp': time.time()})

if __name__ == '__main__':
    main()