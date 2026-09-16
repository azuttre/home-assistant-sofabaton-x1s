"""Does this host answer a SYN to a closed port with a refusal?

A Sofabaton hub only gives up its dial-back loop on a refused
connection. A host whose firewall runs in stealth mode (Windows by
default) drops the SYN instead, and then no release/bounce mechanism can
be observed to work from that host. Run this before a bench that relies
on it; it must print "refused".
"""

import socket
import sys
import time

PORT = 45999


def main() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2.0)
    t = time.monotonic()
    try:
        s.connect(("127.0.0.1", PORT))
        print(f"   127.0.0.1:{PORT}: connected?! something is listening; pick another port")
        return 1
    except ConnectionRefusedError:
        print(f"   127.0.0.1:{PORT}: refused after {time.monotonic() - t:.3f}s (good: closed ports answer)")
        return 0
    except OSError as err:
        print(
            f"   127.0.0.1:{PORT}: {type(err).__name__} after {time.monotonic() - t:.2f}s "
            "(BAD: this host does not refuse; the release check cannot pass here)"
        )
        return 2
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
