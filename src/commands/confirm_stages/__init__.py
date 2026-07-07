"""Decomposed support modules for ``src/commands/confirm.py`` (D-25, Phase 3).

The ``confirm`` command is the critical transaction boundary where proposal
validation, quality-gate evaluation, fact-store shadow-writes, archive snapshot
creation, and baseline promotion meet. Per specs/debt.md D-25 it is being
reduced from a God Module to a thin facade over focused support modules. This
package holds the extracted, self-contained pieces; the transactional write path
itself stays in ``confirm.py`` until its own characterized slice.
"""
