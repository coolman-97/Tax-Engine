"""stand-behind: a deterministic real-estate tax engine.

The engine is pure: no I/O, no network, no database. It takes facts that carry
their own provenance and returns numbers that carry their own derivation.

    from standbehind.fixtures import build_household, build_strategies, MARKET
    from standbehind.simulate import simulate

    result = simulate(build_household(), build_strategies()[0], MARKET)
    result.ledger_hash  # replaying produces the same hash, byte for byte
"""

__version__ = "0.1.0"
