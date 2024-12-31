#!/usr/bin/env python3

# clang-tidy review
# Copyright (c) 2020 Peter Hill
# SPDX-License-Identifier: MIT
# See LICENSE for more information

import argparse
import pprint
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

from clang_tidy_review import (
    MAX_ANNOTATIONS,
    PRReview,
    PullRequest,
    add_auth_arguments,
    bool_argument,
    convert_comment_to_annotations,
    create_review,
    cull_comments,
    decorate_comments,
    fix_absolute_paths,
    get_auth_from_arguments,
    message_group,
    post_annotations,
    post_review,
    save_metadata,
    set_output,
    strip_enclosing_quotes,
)

BAD_CHARS_APT_PACKAGES_PATTERN = "[;&|($]"


def main():
    parser = argparse.ArgumentParser(
        description="Create a review from clang-tidy warnings"
    )
    parser.add_argument("--repo", help="Repo name in form 'owner/repo'")
    parser.add_argument("--pr", help="PR number", type=int)
    parser.add_argument(
        "--clang_tidy_binary",
        help="clang-tidy binary",
        default="clang-tidy-14",
        type=Path,
    )
    parser.add_argument(
        "--build_dir", help="Directory with compile_commands.json", default="."
    )
    parser.add_argument(
        "--base_dir",
        help="Absolute path of initial working directory if compile_commands.json generated outside of Action",
        default=".",
    )
    parser.add_argument(
        "--clang_tidy_checks",
        help="checks argument",
        default="'-*,performance-*,readability-*,bugprone-*,clang-analyzer-*,cppcoreguidelines-*,mpi-*,misc-*'",
    )
    parser.add_argument(
        "--config_file",
        help="Path to .clang-tidy config file. If not empty, takes precedence over --clang_tidy_checks",
        default="",
    )
    parser.add_argument(
        "--include",
        help="Comma-separated list of files or patterns to include",
        type=str,
        nargs="?",
        default="*.[ch],*.[ch]xx,*.[ch]pp,*.[ch]++,*.cc,*.hh",
    )
    parser.add_argument(
        "--exclude",
        help="Comma-separated list of files or patterns to exclude",
        nargs="?",
        default="",
    )
    parser.add_argument(
        "--apt-packages",
        help="Comma-separated list of apt packages to install",
        type=str,
        default="",
    )
    parser.add_argument(
        "--cmake-command",
        help="If set, run CMake as part of the action with this command",
        type=str,
        default="",
    )
    parser.add_argument(
        "--max-comments",
        help="Maximum number of comments to post at once",
        type=int,
        default=25,
    )
    parser.add_argument(
        "--lgtm-comment-body",
        help="Message to post on PR if no issues are found. An empty string will post no LGTM comment.",
        type=str,
        default='clang-tidy review says "All clean, LGTM! :+1:"',
    )
    parser.add_argument(
        "--split_workflow",
        help=(
            "Only generate but don't post the review, leaving it for the second workflow. "
            "Relevant when receiving PRs from forks that don't have the required permissions to post reviews."
        ),
        type=bool_argument,
        default=False,
    )
    parser.add_argument(
        "--annotations",
        help="Use annotations instead of comments",
        type=bool_argument,
        default=False,
    )
    parser.add_argument(
        "-j",
        "--parallel",
        help="Number of tidy instances to be run in parallel.",
        type=int,
        default=0,
    )
    parser.add_argument(
        "--dry-run", help="Run and generate review, but don't post", action="store_true"
    )
    add_auth_arguments(parser)

    args = parser.parse_args()

    # Remove any enclosing quotes and extra whitespace
    exclude = strip_enclosing_quotes(args.exclude).split(",")
    include = strip_enclosing_quotes(args.include).split(",")

    if args.apt_packages:
        # Try to make sure only 'apt install' is run
        apt_packages = re.split(BAD_CHARS_APT_PACKAGES_PATTERN, args.apt_packages)[
            0
        ].split(",")
        apt_packages = [pkg.strip() for pkg in apt_packages]
        with message_group(f"Installing additional packages: {apt_packages}"):
            subprocess.run(["apt-get", "update"], check=True)
            subprocess.run(
                ["apt-get", "install", "-y", "--no-install-recommends", *apt_packages],
                check=True,
            )

    build_compile_commands = f"{args.build_dir}/compile_commands.json"

    cmake_command = strip_enclosing_quotes(args.cmake_command)

    # If we run CMake as part of the action, then we know the paths in
    # the compile_commands.json file are going to be correct
    if cmake_command:
        with message_group(f"Running cmake: {cmake_command}"):
            subprocess.run(cmake_command, shell=True, check=True)

    elif Path(build_compile_commands).exists():
        fix_absolute_paths(build_compile_commands, args.base_dir)

    pull_request = PullRequest(args.repo, args.pr, get_auth_from_arguments(args))

    review = create_review(
        pull_request,
        args.build_dir,
        args.clang_tidy_checks,
        args.clang_tidy_binary,
        args.config_file,
        args.parallel,
        include,
        exclude,
    )

    with message_group("Saving metadata"):
        save_metadata(args.pr)

    if args.split_workflow:
        total_comments = 0 if review is None else len(review["comments"])
        set_output("total_comments", str(total_comments))
        print("split_workflow is enabled, not posting review")
        return

    if args.annotations:
        post_annotations(pull_request, review)
    else:
        lgtm_comment_body = strip_enclosing_quotes(args.lgtm_comment_body)
        post_review(
            pull_request, review, args.max_comments, lgtm_comment_body, args.dry_run
        )


def post_review(
    pull_request: PullRequest,
    review: Optional[PRReview],
    max_comments: int,
    lgtm_comment_body: str,
    dry_run: bool,
) -> int:
    print(
        "Created the following review:\n", pprint.pformat(review, width=130), flush=True
    )

    if not review or review["comments"] == []:
        print("No warnings to report, LGTM!")
        if not dry_run:
            pull_request.post_lgtm_comment(lgtm_comment_body)
        return 0

    total_comments = len(review["comments"])

    set_output("total_comments", str(total_comments))

    decorated_review = decorate_comments(review)

    print("Removing already posted or extra comments", flush=True)
    trimmed_review = cull_comments(pull_request, decorated_review, max_comments)

    if not trimmed_review["comments"]:
        print("Everything already posted!")
        return total_comments

    if dry_run:
        pprint.pprint(review, width=130)
        return total_comments

    print("Posting the review:\n", pprint.pformat(trimmed_review), flush=True)
    pull_request.post_review(trimmed_review)

    return total_comments


def post_annotations(
    pull_request: PullRequest, review: Optional[PRReview]
) -> Optional[int]:
    """Post the first 10 comments in the review as annotations"""

    body: dict[str, Any] = {
        "name": "clang-tidy-review",
        "head_sha": pull_request.pull_request.head.sha,
        "status": "completed",
        "conclusion": "success",
    }

    if review is None:
        return None

    if review["comments"] == []:
        print("No warnings to report, LGTM!")
        pull_request.post_annotations(body)

    comments = []
    for comment in review["comments"]:
        first_line = comment["body"].splitlines()[0]
        comments.append(
            f"{comment['path']}:{comment.get('start_line', comment.get('line', 0))}: {first_line}"
        )

    total_comments = len(review["comments"])

    body["conclusion"] = "neutral"
    body["output"] = {
        "title": "clang-tidy-review",
        "summary": f"There were {total_comments} warnings",
        "text": "\n".join(comments),
        "annotations": [
            convert_comment_to_annotations(comment)
            for comment in review["comments"][:MAX_ANNOTATIONS]
        ],
    }

    pull_request.post_annotations(body)
    return total_comments


if __name__ == "__main__":
    main()
