"""A stand-in for one of pcatgl's display helpers.

Its whole job is to carry the argv it was given (so the plugin's token
matching sees a real /proc/<pid>/cmdline) and, with --listen, to hold a
listening socket the way websockify does.
"""
import socket, sys, time

port = None
if "--listen" in sys.argv:
    port = int(sys.argv[sys.argv.index("--listen") + 1])
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(5)
time.sleep(600)
