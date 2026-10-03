Production Atlas and Seraph now consume the shared gateway routing policy.
They no longer inject legacy batch ceilings or model aliases in policy mode.
Review receipts must match the policy model, transport and selected backend
family, while exact source, claim, recommendation and live-unit checks remain.
Both production dispatch wrappers allow 150 seconds, including 25 seconds
beyond the shared 125-second dispatch budget for cleanup. Legacy operation
without a production policy is unchanged.
