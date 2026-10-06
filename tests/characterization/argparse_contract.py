"""Check interpreter-formatted output without cross-version byte comparisons."""

import argparse

from sumbi.cli import parser

from .corpus import CASES


def output_stream(case):
    if "--help" in case["argv"]:
        return "stdout.txt"
    if case["expected_exit"] == 2:
        return "stderr.txt"
    return None


def is_argparse_artifact(name):
    case_name, artifact = name.split("/", 1)
    case = next(case for case in CASES if case["name"] == case_name)
    return artifact == output_stream(case)


def check_output(case, artifacts):
    stream = output_stream(case)
    if stream is None:
        return
    if artifacts["exit-code.txt"] != str(case["expected_exit"]) + "\n":
        raise AssertionError(case["name"] + ": unexpected argparse exit status")
    other = "stderr.txt" if stream == "stdout.txt" else "stdout.txt"
    if artifacts[other] or not artifacts[stream]:
        raise AssertionError(case["name"] + ": unexpected argparse output stream")
    selected = parser()
    # Product validation errors call the root parser's error method. The
    # register missing-arguments case fails in its subparser during parsing.
    if case["command"] != "root" and (stream == "stdout.txt" or case["name"] == "register-missing-arguments"):
        commands = next(action.choices for action in selected._actions if getattr(action, "choices", None))
        selected = commands[case["command"]]
    text = artifacts[stream]
    names = {selected.prog}
    for action in selected._actions:
        if action.help == argparse.SUPPRESS:
            continue
        # Help lists every alias; usage lists the first spelling of each option.
        names.update(action.option_strings if stream == "stdout.txt" else action.option_strings[:1])
        if isinstance(getattr(action, "choices", None), dict):
            names.update(action.choices)
    for name in sorted(names):
        if name not in text:
            raise AssertionError(case["name"] + ": argparse output missing " + name)
