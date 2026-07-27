import sys, asyncio
sys.path.insert(0, "src")

from aether_search.agent import AuxAgent

agent = AuxAgent()
print("active connectors (no keys):", [c.name for c in agent.connectors])

qs = asyncio.run(agent.rewrite("test query"))
assert qs == ["test query"], qs
print("rewrite degrade OK")

from aether_search.connectors.parallel import ParallelConnector
from aether_search.connectors.brave import BraveConnector
print("multi_query:", "parallel=", ParallelConnector.multi_query, "brave=", BraveConnector.multi_query)

from aether_search.grok_provider import GrokProvider
print("grok configured (no env):", GrokProvider().is_configured())

from aether_search import server
print("server imports OK")

from aether_search.fusion import rrf_fusion
from aether_search.connectors.base import Source
a = [Source(title="A", url="http://x/1", content=""), Source(title="B", url="http://x/2", content="")]
b = [Source(title="B2", url="http://x/2", content=""), Source(title="C", url="http://x/3", content="")]
f = rrf_fusion([a, b], top_k=5)
assert f[0].url == "http://x/2" and len(f) == 3
print("fusion OK")

# no keys: aux channel must be empty and run() degrades to a no-op
assert agent.connectors == [], [c.name for c in agent.connectors]

async def smoke():
    sources, stats = await agent.run("python asyncio semaphore example")
    assert sources == [] and stats.get("hits", 0) == 0, stats
    print("aux no-op without keys:", stats.get("connectors"))

asyncio.run(smoke())
print("ALL OK")
