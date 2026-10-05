"""
stdfcompanion.cli
~~~~~~~~~~~~~~~~~

Main Click-based CLI entry point.

Usage examples
--------------
Merge two files::

    stdfcompanion merge file1.stdf file2.stdf -o merged.stdf

Check a file for errors::

    stdfcompanion check file.stdf
    stdfcompanion check file.stdf --warnings --json

Repair a file::

    stdfcompanion repair file.stdf -o repaired.stdf
    stdfcompanion repair file.stdf -o repaired.stdf --verbose

Show version::

    stdfcompanion --version
"""

from __future__ import annotations

import json
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

    \b
    Example:
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


# ---------------------------------------------------------------------------
# check sub-command
# ---------------------------------------------------------------------------

@main.command("check")
@click.argument(
    "inputs",
    nargs=-1,
    required=True,
    type=click.Path(exists=True, dir_okay=False, readable=True),
)
@click.option(
    "--warnings/--no-warnings",
    default=True,
    show_default=True,
    help="Include WARNING-level issues in output.",
)
@click.option(
    "--info/--no-info",
    default=False,
    show_default=True,
    help="Include INFO-level issues in output.",
)
@click.option(
    "--json", "as_json",
    is_flag=True,
    default=False,
    help="Output results as JSON (useful for scripting).",
)
@click.option(
    "-v", "--verbose",
    is_flag=True,
    default=False,
    help="Print progress information.",
)
def check_cmd(inputs, warnings, info, as_json, verbose):
    """Check one or more STDF files for errors and structural issues.

    Exits with code 0 if all files pass (no ERROR-level issues),
    non-zero otherwise.

    \b
    Example:
        stdfcompanion check file.stdf
        stdfcompanion check a.stdf b.stdf --warnings --json
    """
    from stdfcompanion.commands.check import check_stdf, Severity

    any_error = False
    all_results = []

    for path in inputs:
        result = check_stdf(path, verbose=verbose)
        all_results.append(result)
        if not result.ok:
            any_error = True

    if as_json:
        output = []
        for r in all_results:
            issues = []
            for i in r.issues:
                if i.severity == Severity.ERROR:
                    pass
                elif i.severity == Severity.WARNING and not warnings:
                    continue
                elif i.severity == Severity.INFO and not info:
                    continue
                issues.append({
                    "code":     i.code,
                    "severity": i.severity.value,
                    "message":  i.message,
                    "offset":   i.offset,
                    "record":   i.record,
                })
            output.append({
                "file":    r.path,
                "ok":      r.ok,
                "summary": r.summary(),
                "issues":  issues,
            })
        click.echo(json.dumps(output, indent=2))
    else:
        for result in all_results:
            click.echo(f"\n{'='*60}")
            click.echo(f"File: {result.path}")
            click.echo(f"{'='*60}")

            shown = 0
            for issue in result.issues:
                if issue.severity == Severity.ERROR:
                    click.echo(str(issue))
                    shown += 1
                elif issue.severity == Severity.WARNING and warnings:
                    click.echo(str(issue))
                    shown += 1
                elif issue.severity == Severity.INFO and info:
                    click.echo(str(issue))
                    shown += 1

            if shown == 0:
                click.echo("  (no issues found at selected severity levels)")

            click.echo(f"\n  {result.summary()}")

    sys.exit(1 if any_error else 0)


# ---------------------------------------------------------------------------
# repair sub-command
# ---------------------------------------------------------------------------

@main.command("repair")
@click.argument(
    "input",
    type=click.Path(exists=True, dir_okay=False, readable=True),
)
@click.option(
    "-o", "--output",
    required=True,
    type=click.Path(dir_okay=False, writable=True),
    help="Output path for the repaired STDF file.",
)
@click.option(
    "--check-after/--no-check-after",
    default=True,
    show_default=True,
    help="Run check on the repaired file and show remaining issues.",
)
@click.option(
    "-v", "--verbose",
    is_flag=True,
    default=False,
    help="Print each repair action applied.",
)
def repair_cmd(input, output, check_after, verbose):
    """Attempt to repair a malformed STDF file.

    Reads INPUT, applies all applicable automatic repairs, and writes the
    result to OUTPUT.  The original file is never modified.

    \b
    Automatic repairs include:
      - Force FAR CPU_TYPE=2 and STDF_VER=4
      - Insert missing MIR or MRR
      - Move MRR to end of file
      - Close unmatched PIR/PRR, WIR/WRR pairs
      - Fix zero or inverted timestamps

    \b
    Example:
        stdfcompanion repair bad.stdf -o fixed.stdf
        stdfcompanion repair bad.stdf -o fixed.stdf --verbose
    """
    from stdfcompanion.commands.repair import repair_stdf
    from stdfcompanion.commands.check  import check_stdf, Severity

    if verbose:
        click.echo(f"stdfcompanion repair  v{__version__}")
        click.echo(f"  Input  : {input}")
        click.echo(f"  Output : {output}")

    try:
        result = repair_stdf(input, output, verbose=verbose)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if result.repaired:
        click.echo(f"\n{len(result.actions)} repair(s) applied:")
        for act in result.actions:
            click.echo(f"  {act}")
    else:
        click.echo("No repairs were necessary.")

    click.echo(f"\n{result.summary()}")
    click.echo(f"Repaired file written to: {output}")

    if check_after:
        click.echo("\nPost-repair check:")
        post = check_stdf(output, verbose=False)
        if post.ok:
            click.echo("  No errors remain.")
        else:
            for issue in post.issues:
                if issue.severity == Severity.ERROR:
                    click.echo(f"  {issue}")
        click.echo(f"  {post.summary()}")

    sys.exit(0)


# ---------------------------------------------------------------------------
# inspect sub-command
# ---------------------------------------------------------------------------

@main.command("inspect")
@click.argument(
    "input",
    type=click.Path(exists=True, dir_okay=False, readable=True),
)
@click.option(
    "-r", "--record",
    "record_types",
    multiple=True,
    metavar="TYPE",
    help="Only show this record type (e.g. PTR, MIR). Repeat for multiple types.",
)
@click.option(
    "--head",
    "head_filter",
    type=int,
    default=None,
    help="Filter records by HEAD_NUM.",
)
@click.option(
    "--site",
    "site_filter",
    type=int,
    default=None,
    help="Filter records by SITE_NUM.",
)
@click.option(
    "--part",
    "part_filter",
    type=int,
    default=None,
    help="Only show records in the Nth PIR/PRR block (1-based).",
)
@click.option(
    "-n", "--limit",
    type=int,
    default=None,
    help="Stop after N matching records.",
)
@click.option(
    "-o", "--output",
    "output_file",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="Write output to a file instead of stdout.",
)
@click.option(
    "--summary",
    "output_format",
    flag_value="summary",
    default=False,
    help="Print a one-line-per-type summary table.",
)
@click.option(
    "--json",
    "output_format",
    flag_value="json",
    help="Output as JSON array.",
)
@click.option(
    "--text",
    "output_format",
    flag_value="text",
    default=True,
    help="Output as human-readable text (default).",
)
@click.option(
    "-v", "--verbose",
    is_flag=True,
    default=False,
    help="Print progress information.",
)
def inspect_cmd(input, record_types, head_filter, site_filter,
                part_filter, limit, output_file, output_format, verbose):
    """Dump the contents of an STDF file in human-readable form.

    \b
    Examples:
        # Full text dump
        stdfcompanion inspect file.stdf

        # Summary table (record counts)
        stdfcompanion inspect file.stdf --summary

        # Only PTR records for head=1, site=1
        stdfcompanion inspect file.stdf -r PTR --head 1 --site 1

        # First PIR/PRR block in detail
        stdfcompanion inspect file.stdf --part 1

        # First 20 records as JSON
        stdfcompanion inspect file.stdf --json -n 20

        # Write full dump to a text file
        stdfcompanion inspect file.stdf -o dump.txt

        # Only MIR and MRR
        stdfcompanion inspect file.stdf -r MIR -r MRR
    """
    from stdfcompanion.commands.inspect import inspect_stdf

    try:
        output = inspect_stdf(
            input,
            record_types=list(record_types) if record_types else None,
            head_filter=head_filter,
            site_filter=site_filter,
            part_filter=part_filter,
            limit=limit,
            output_format=output_format,
            verbose=verbose,
        )
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(output)
        click.echo(f"Output written to: {output_file}")
    else:
        click.echo(output, nl=False)


if __name__ == "__main__":
    main()
