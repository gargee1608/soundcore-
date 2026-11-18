from soundcore.audio.detect import guess_kind, parse_alsa_listing

SAMPLE = """
**** List of CAPTURE Hardware Devices ****
card 0: Headphones [bcm2835 Headphones], device 0: bcm2835 Headphones [bcm2835 Headphones]
  Subdevices: 1/1
card 1: CODEC [USB Audio CODEC], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
card 2: Headset [Plantronics Headset], device 0: USB Audio [USB Audio]
"""


def test_parse_capture_cards():
    devices = parse_alsa_listing(SAMPLE, direction="in")
    assert devices[0].id == "alsa:local:Headphones:pcm0"
    assert devices[0].kind == "jack"
    assert devices[0].hw == "plughw:0,0"
    assert not devices[0].name.startswith("3.5")
    assert "Headphones" in devices[0].name
    assert devices[1].kind == "usb"
    assert devices[1].id == "alsa:usb-cardid:CODEC:pcm0"
    assert devices[1].usb_id == "usb-cardid:CODEC:pcm0"
    assert devices[1].hw == "plughw:1,0"
    assert "card1" not in devices[1].id
    assert guess_kind("USB Audio CODEC", 3) == "usb"
