# ADR-0005 — Repository location; Desktop is never a git repo

**Status:** Accepted · **Date:** 2026-07-27

## Context

The invocation working directory was `~/Desktop`. Phase 0 required creating a git
branch for the build, which naively implies `git init` in the working directory.

## Finding

`~/Desktop` is not a git repository, and must not become one. It is a working desktop
holding unrelated personal and professional material.

Running `git init` there would stage tens of thousands of unrelated files, create a
plausible route for confidential material to reach a remote, and leave a `.git`
directory the owner did not ask for at the root of their desktop.

The established convention on this machine is one self-contained git repository per
project directory, never a repository spanning unrelated work.

## Decision

The repository is a self-contained git repo at `~/Desktop/Meridian`, separate from
`~/Desktop` itself, developed on branch `build/foundation-vertical-slice`.

`.gitignore` and `.env.example` were written **before** `git init` and the first commit,
so no credential or unrelated file can enter history from the first commit onward. The
initial commit was verified clean by a secret-pattern scan over staged content.

## Consequence

Nothing outside the repository directory is read, modified or tracked. Any pre-existing
personal Obsidian vault on the machine is **not** touched — Meridian generates into its
own in-repo vault, and `MERIDIAN_VAULT_PATH` is configurable if it should ever point
elsewhere deliberately.
