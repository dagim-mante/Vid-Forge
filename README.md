# Vid-Forge

Vid-Forge is a staged AI filmmaking pipeline. It keeps storyboard creation, candidate keyframes, per-shot video generation, narration, music, and final film assembly as separate steps.

The `video` stage reads selected keyframes and writes individual clips to `projects/<project>/outputs/shots/`. Run `narration` to create the timed voice track and subtitles, then `assemble` adds the storyboard title and credits, combines available shots in order, and writes landscape 1280x720 and portrait 720x1280 films. Missing shot clips appear as black intervals so narration timing stays aligned.

```powershell
vid-forge video project=tibeb --dry-run
vid-forge keyframes project=tibeb --shot shot_02
vid-forge keyframes project=tibeb --shot shot_02 --candidate-count 5
vid-forge video project=tibeb --shot shot_02
vid-forge video project=tibeb --validate-only
vid-forge assemble project=tibeb --dry-run
vid-forge assemble project=tibeb
vid-forge assemble project=tibeb --transition crossfade --transition-duration 0.75
```

`--candidate-count` updates `keyframe.candidates` in the storyboard for the selected scope (1–6), so generated candidates remain valid storyboard selections.

See [the video assembly guide](pipeline/video-assemble/README.md) for Kaggle configuration, model requirements, cache behavior, and recovery instructions.

See [the final film assembly guide](pipeline/assembly/README.md) for FFmpeg setup, transitions, output protection, and the assembly manifest.
