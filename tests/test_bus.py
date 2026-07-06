import asyncio

from meshradio.bus import EventBus


async def test_publish_subscribe(bus: EventBus):
    sub = bus.subscribe("a.topic")
    bus.publish("a.topic", {"n": 1})
    topic, payload = await asyncio.wait_for(sub.get(), 1)
    assert topic == "a.topic"
    assert payload == {"n": 1}


async def test_topic_filtering(bus: EventBus):
    sub = bus.subscribe("wanted")
    bus.publish("unwanted", {"n": 1})
    bus.publish("wanted", {"n": 2})
    _, payload = await asyncio.wait_for(sub.get(), 1)
    assert payload == {"n": 2}


async def test_subscribe_all(bus: EventBus):
    sub = bus.subscribe()
    bus.publish("x")
    bus.publish("y")
    assert (await sub.get())[0] == "x"
    assert (await sub.get())[0] == "y"


async def test_multiple_subscribers(bus: EventBus):
    s1, s2 = bus.subscribe("t"), bus.subscribe("t")
    bus.publish("t", {"n": 1})
    assert (await s1.get())[1] == {"n": 1}
    assert (await s2.get())[1] == {"n": 1}


async def test_close_unsubscribes(bus: EventBus):
    sub = bus.subscribe("t")
    sub.close()
    bus.publish("t")
    assert sub.queue.empty()


async def test_slow_subscriber_drops_oldest(bus: EventBus):
    sub = bus.subscribe("t")
    sub.queue = asyncio.Queue(maxsize=2)
    for n in range(3):
        bus.publish("t", {"n": n})
    assert (await sub.get())[1] == {"n": 1}
    assert (await sub.get())[1] == {"n": 2}
