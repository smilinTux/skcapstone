# SKCP-00F13 preview and provenance repair evidence

Card: `45acd0eb`
Base revision: `dd62165a8326006288bfd46c0bbdbfbe79aa434e`
Date: 2026-08-24

## Active visual

The append-only V2.2 HTML file is the active control-plane estate-pulse
prototype after this repair:

- Active path: `docs/wireframes/control-plane-estate-pulse-v2.2.html`
- Active SHA-256:
  `f4722b9c77c8c6b1451aec7c59a4ac8c133635793e0ae4a1c558d9b09c128ce5`
- Preserved released V2.1 SHA-256:
  `b3636c0017f5f3289094873b0ebed03806fbaa3bbc92bc705e03e0f7c32037c9`

V2.1 remains immutable evidence. V2.2 changes only the prototype label,
unavailable-state option, fail-closed state selection, and URL initialization.
The normal on-screen Review next step trigger still selects the explicit ready
state. The prototype contains no dispatch implementation.

## Approval authority projection

- Path:
  `docs/approval/SKCP-00-V1.1.3-AUTHORITY-PROJECTION-v1.json`
- SHA-256:
  `0e2fd4336f0ac58da3c0a50dcae11ecae5a233f2a35776b71aaea6d773780d5a`

The projection does not grant authority. It reports the exact human-source
boundary. V1 receipt SHA-256 `d6c5a024...` and H4 are authoritative. V2, the
unsupported H5 quote, and the dependent R4 PASS are non-authoritative. All
historical bytes remain present.

The current board topology also remains visible as a limitation. Card
`d12b8951` was observed done and card `94cbf19a` was observed doing after both
had depended on unsupported R4. Both now include fresh review gate `847e250a`.
This evidence does not rewrite their history or make them eligible.

## Real Chrome CDP qualification

Standalone command:

```bash
node scripts/qualify_control_plane_preview_cdp.mjs
```

Qualifier SHA-256:
`e8a64ffea1ed055f331095ca437e6be26966444765c0ba114a19c35bbeec816b`

Google Chrome `151.0.7922.108` returned `PASS`:

- 4 URL boundary cases passed: unknown, missing, blank, and encoded whitespace.
- All 6 declared states retained their intended status, selected value,
  disabled property, and `aria-disabled` value.
- The normal explicit ready trigger remained enabled.
- Clicking the synthetic ready control produced the no-queue prototype notice.
- Network observation found 0 non-GET and 0 external requests after the click.
- Runtime observation found 0 exceptions.

The focused test deliberately applies the repaired static assertions to V2.1
and observes failure, then applies them to V2.2 and observes success.

## Boundary

This repair does not authorize deployment, activation, restart, external
action, protected Matter access, board reconciliation, or gate bypass. Fresh
independent review `847e250a` remains required.
