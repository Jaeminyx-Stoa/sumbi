"""Synthetic inventory fixture; it is never imported."""


def tests(session):
    session.run("pytest")
