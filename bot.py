"""Discord 상시 봇 — 메시지를 Gateway(웹소켓)로 바로 받는다. 1분 폴링(trend.py poll)을 대신한다.

  ~/.local/share/trend-automation/venv/bin/python bot.py      (systemd trend-bot.service가 띄운다)

- 사용자(DISCORD_OWNER_ID)가 #브리핑 · #인사이트에 쓴 말만 처리한다. 처리는 trend.handle()이 한다.
- 처리(claude -p)는 몇 분 걸리므로 별도 작업 스레드 하나가 순서대로 한다. 받기는 막히지 않는다.
- 켜질 때 꺼져 있던 동안 쌓인 메시지를 trend.poll()로 한 번 따라잡는다(state/poll.json의 채널별 마지막 ID).
- 연결이 끊기면 discord.py가 다시 붙고, 프로세스가 죽으면 systemd가 다시 띄운다(Restart=always).
"""
import queue
import threading

import discord

import trend

jobs = queue.Queue()


def worker():
    """작업을 하나씩 처리한다. "catchup"은 꺼져 있던 동안의 메시지를 따라잡는 일이다."""
    while True:
        cid, name, mid, text = jobs.get()
        try:
            if cid == "catchup":
                trend.poll()
                continue
            if int(mid) <= int(trend.load_json(trend.STATE / "poll.json", {}).get(cid, "0")):
                continue  # 따라잡기(poll)가 이미 처리한 메시지
            trend.REPLY.update(cid=cid, name=name)
            trend.handle(text, name)
        except Exception as e:  # noqa: BLE001 — 한 메시지가 실패해도 봇은 계속 산다
            trend.log(f"ERROR bot {e}")
        finally:
            trend.REPLY.clear()
            if cid != "catchup":
                seen = trend.load_json(trend.STATE / "poll.json", {})
                if int(mid) > int(seen.get(cid, "0")):
                    seen[cid] = mid
                    trend.save_json(trend.STATE / "poll.json", seen)


def main():
    owner = trend.os.environ["DISCORD_OWNER_ID"]
    names = {cid: name for name, cid in trend.channels().items()}
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        trend.log(f"bot ready as {client.user} · 채널 {list(names.values())}")
        jobs.put(("catchup", None, "0", None))

    @client.event
    async def on_message(m):
        cid = str(m.channel.id)
        if str(m.author.id) == owner and cid in names:
            jobs.put((cid, names[cid], str(m.id), m.content.strip()))

    threading.Thread(target=worker, daemon=True).start()
    client.run(trend.os.environ["DISCORD_BOT_TOKEN"], log_handler=None)


if __name__ == "__main__":
    main()
