Remote initial profile qualification now recovers a trusted worker preflight
failure caused by a remote predecessor in the sealed plan chain. Recovery is
limited to the exact unchanged plan and runs the next immutable successor with
the remote predecessor validated by the scoped native worker wrapper.
