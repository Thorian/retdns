"""Resolve through a selected server, optionally hand the result to NomadPL."""

import argparse

import RNS

from retdns import Cache, Resolver

parser = argparse.ArgumentParser()
parser.add_argument("server", help="retdns.server destination address")
parser.add_argument("name", help="Registered name, for example home.ret")
parser.add_argument("--rns-config")
args = parser.parse_args()
RNS.Reticulum(configdir=args.rns_config)
with Resolver(args.server, Cache("state/client-cache.sqlite3")) as resolver:
    address = resolver.resolve(args.name)
    print("Forward:", args.name, "->", address)
    print("Reverse:", address, "->", resolver.reverse(address))
    # Optional separate dependency:
    # from nomadpl import Client, Request
    # client = Client(address)
    # try:
    #     response = client.request(Request("GET", "/"))
    #     print(response.status)
    # finally:
    #     client.close()
