# tools/ship_logs.py
import re, sys, json, httpx, asyncio, datetime as dt

LINE = re.compile(
    r"\[(?P<sym>[A-Z]+USDT)\]\s+(?P<kind>Heartbeat OK|No-trade \((?P<reason>[a-z\-]+)\))"
    r"(?:.*?k=(?P<k>[\d\.]+))?(?:\s+adx=(?P<adx>[\d\.]+))?(?:\s+atr=(?P<atr>[\d\.]+))?"
)

# BasE ENV 
BASE = "http://localhost:8000"
async def main():
    async with httpx.AsyncClient(timeout=5) as client:
        for raw in sys.stdin:
            m = LINE.search(raw)
            if not m: 
                continue
            d = m.groupdict()
            ev = {
                "symbol": d["sym"],
                "ts": dt.datetime.utcnow().isoformat()+"Z",
            }
            if d["kind"].startswith("Heartbeat"):
                ev["type"] = "heartbeat"; ev["reason"] = "ok"
            else:
                ev["type"] = "decision"; ev["decision"] = "no-trade"; ev["reason"] = d["reason"]
            for k in ("k","adx","atr"):
                if d.get(k):
                    ev[k] = float(d[k])
            await client.post(f"{BASE}/engine/event", json=ev)

if __name__ == "__main__":
    asyncio.run(main())