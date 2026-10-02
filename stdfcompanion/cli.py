"""
stdfcompanion.cli
~~~~~~~~~~~~~~~~~

Main Click-based CLI entry point.

Usage examples
--------------
Merge two files::

    stdfcompanion merge file1.stdf file2.stdf -o merged.stdf

Merge with verbose output::

    stdfcompanion merge a.stdf b.stdf c.stdf -o out.stdf --verbose

Show version::

    stdfcompanion --version
"""

from __future__ import annotations

import sys
import click

from stdfcompanion import __version__


# ---------------------------------------------------------------------------
# Root command group
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(__version__, prog_name="stdfcompanion")
def main():
    """stdfcompanion – STDF file parser and manipulation tool."""


# ---------------------------------------------------------------------------
# merge sub-command
# ---------------------------------------------------------------------------

@main.command("merge")
@click.argument(
    "inputs",
    nargs=-1,
    required=True,
    type=click.Path(exists=True, dir_okay=False, readable=True),
)
@click.option(
    "-o", "--output",
    required=True,
    type=click.Path(dir_okay=False, writable=True),
    help="Output STDF file path.",
)
@click.option(
    "-v", "--verbose",
    is_flag=True,
    default=False,
    help="Print progress information.",
)
def merge_cmd(inputs, output, verbose):
    """Merge two or more STDF files into a single output file.

    INPUTS is one or more input STDF files (at least 2 required).

    Example:

    \b
        stdfcompanion merge lot1.stdf lot2.stdf -o merged.stdf
        stdfcompanion merge a.stdf b.stdf c.stdf -o combined.stdf --verbose
    """
    if len(inputs) < 2:
        raise click.UsageError("merge requires at least 2 input files.")

    from stdfcompanion.commands.merge import merge_stdf

    if verbose:
        click.echo(f"stdfcompanion merge  v{__version__}")
        click.echo(f"  Input files  : {len(inputs)}")
        for p in inputs:
            click.echo(f"    {p}")
        click.echo(f"  Output file  : {output}")

    try:
        merge_stdf(inputs, output, verbose=verbose)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    click.echo(f"Merged {len(inputs)} files -> {output}")


if __name__ == "__main__":
    main()
