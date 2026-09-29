"""[Agentic][HITL] Long-term memory built from clinician decisions.

`precedents`    — de-identified, vector-indexed record of how clinicians ruled
                  on past escalations; HITL retrieves the k nearest.
`retrain_queue` — clinician disagreements, queued in a train.py-ready format.
"""
