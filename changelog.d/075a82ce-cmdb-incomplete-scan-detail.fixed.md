A refused network apply on an incomplete scan now names each incomplete
target with its failing or unavailable collectors, plus deadline, failure
budget and declared collector failures. The refusal writes no artifact, so the
old "inspect collector health" message left no record of the cause.
