"""[Microservices] One agent, one container.

wire.py       CaseState / AgentMessage <-> JSON, and the per-agent write lane
workers.py    which worker class each container runs, and how it is built
agent_app.py  the HTTP face of one agent (`/v1/invoke`)
remote.py     the gateway side: RemoteAgent stands in for a worker object
llm_app.py    llm-gateway — the only container holding provider keys
rag_app.py    rag-service — corpus index + embedder, shared by every agent
serve.py      container entrypoint: `python -m app.microservices.serve <service>`
"""
