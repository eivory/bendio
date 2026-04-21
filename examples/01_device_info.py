"""Connect to a radio, print device info, read a few channels."""
from __future__ import annotations

import asyncio
import sys

from bendio import Radio


async def main(address: str) -> None:
    async with Radio(address) as radio:
        info = await radio.device_info()
        print("Device info:", info)

        for i in range(5):
            ch = await radio.read_rf_ch(i)
            print(f"  ch[{i}]: {ch}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python 01_device_info.py <BT_ADDR>")
        sys.exit(2)
    asyncio.run(main(sys.argv[1]))
