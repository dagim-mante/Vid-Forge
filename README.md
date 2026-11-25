# Vid-Forge

Vid-Forge is a staged AI filmmaking pipeline. It keeps storyboard creation, candidate keyframes, per-shot video generation, narration, music, and final film assembly as separate steps.

Current CLI stages include `storyboard`, `keyframes`, `validate`, `narration`, `radio_cut`, and `video`. The keyframes stage generates candidate images; people select them in the storyboard. The video stage reads those selections and writes individual clips to `projects/<project>/outputs/shots/`; it does not assemble the finished film.

```powershell
vid-forge video project=tibeb --dry-run
vid-forge keyframes project=tibeb --shot shot_02
vid-forge keyframes project=tibeb --shot shot_02 --candidate-count 5
vid-forge video project=tibeb --shot shot_02
vid-forge video project=tibeb --validate-only
```

`--candidate-count` updates `keyframe.candidates` in the storyboard for the selected scope (1–6), so generated candidates remain valid storyboard selections.

See [the video assembly guide](pipeline/video-assemble/README.md) for Kaggle configuration, model requirements, cache behavior, and recovery instructions.
