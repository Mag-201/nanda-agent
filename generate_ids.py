import os
from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv())

def generate_agent_ids():
    prefix = os.getenv("AGENT_ID_PREFIX", "default")
    num_agents = int(os.getenv("NUM_AGENTS", "1"))
    registry_url = os.getenv("REGISTRY_URL", "")
    is_official = "nanda-registry.com" in registry_url.lower()
    base = "agentm" if is_official else "agents"
    return [f"{base}{prefix}{i}" for i in range(num_agents)]

if __name__ == "__main__":
    ids = generate_agent_ids()
    print("Agent IDs:", ", ".join(ids))
