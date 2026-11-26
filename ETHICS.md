# ETHICS — consent, cultural care, licences, labelling

## Consent record

| Person | Appears (face/likeness)? | Voiced? | Consent |
|---|---|---|---|
| **Alemu (አለሙ)** | No real person is shown. Alemu is an entirely synthetic, fictional archetype of an elderly Ethiopian highland farmer (aged 70 $\rightarrow$ 85). His face and appearance are AI-generated and not based on any living or deceased individual. | No. Dialogue and voiceover lines are synthesized using prebuilt Gemini TTS voices, not cloned from or modeled after any real speaker's voice. | Not required (entirely synthetic fictional character). If a real person's likeness or voice is ever added in a future revision, verified written informed consent will be recorded prior to generation. |
| **Villagers** | No. Generic background community members depicted in group compositions, profiles, or from behind in dusty traditional cotton attire. | No. Villager lines are synthesized via generic Gemini TTS voice parameters. | Not required (synthetic generic crowd figures). |
| **Young Girl (Final Scene)** | No. A synthetic generic archetype of an 8–9 year old highland girl with traditional braids (*ሹሩባ*). | No. Silent character appearing in the final legacy scene. | Not required (synthetic fictional character). |

---

## Plot Ethics

- **Authentic Highland Agrarian Tradition:** The film depicts an Ethiopian highland rural community enduring historical drought. Traditional cultural elements—circular thatched stone-and-mud homes (*tukuls* ጎጆ ቤቶች), hand-spun cotton garments (*shamma* ሻማ, *gabi* ጋቢ), wooden digging tools (*መቆፈሪያ*), grain storage structures (*ጎተራ*), and woven grass baskets (*ሰፌድ*, *ቅርጫት*)—are represented with care, historical grounding, and respect.
- **Community Framing:** Villagers are framed not as villains or antagonists, but as exhausted human neighbors reacting out of basic survival instinct during extreme famine. Alemu's choice embodies the revered Ethiopian highland virtue of *መጽናት* (patient endurance and generational faith in the land).le is natural: rain returns, the seed germinates in damp soil, and decades of quiet human patience produce a shade tree.
- **Language Purity:** The Amharic narration lines (`text`) were reviewed to ensure natural oral cadence, grammatical coherence, and respectful storytelling register fitting an evening hearth (*እሳት ዳር*).

---

## Private data

- No private family photos, identifiable personal portraits, or private personal data were uploaded to any third-party model or cloud service.
- All reference images in `input/references/` are either publicly accessible cultural references or synthetic concept anchors.
- All prompts sent to Gemini, Cloudflare Workers AI, and Kaggle consist exclusively of public descriptive prose regarding Ethiopian highland geography, climate, and folktale narrative structure.

---

## Licences

| Component | Model / tool | Licence / terms | Output use |
|---|---|---|---|
| **Keyframes** | FLUX.2 klein 4B / 9B via Cloudflare Workers AI | Apache 2.0 (weights); Cloudflare Workers AI Terms of Service | Usable commercially and creatively |
| **Keyframe fallback** | FLUX.1 schnell via Cloudflare Workers AI | Apache 2.0 | Usable |
| **Moving shots** | Wan 2.2 TI2V-5B (Kaggle GPU) | Apache 2.0 | Usable |
| **Narration** | Gemini TTS (`gemini-3.8-flash-lite-tts`, Google AI Studio) | Google Gemini API Terms of Service | Usable with disclosure |
| **Script & Storyboard** | Gemini Flash (`gemini-3.5-flash-lite`) | Google Gemini API Terms of Service | Usable |
| **Score & Audio Bed** | Ethio-acoustic arrangement (*washint*, *krar*, *masinko*, *kebero*) | Royalty-free / open acoustic music assets | Usable |
| **Assembly & Compositing** | FFmpeg (`libx264`, `libass`) | LGPL / GPL tool | Does not restrict or encumber output video |
| **Subtitles & Typography** | Noto Sans Ethiopic / Noto Serif Ethiopic | SIL Open Font License (OFL) | Fully licensed for video embedding |
| **This Repository** | Vid-Forge toolchain and pipeline scripts | MIT License (see `LICENSE`) | Open and permissive |

---

## Labelling plan

- **In the film:**
  - A 5-second end card explicitly discloses:
    > *"This film was made with AI."*  
    > *"ዛሬ የምትዘራው ዘር፣ የነገ ትውልድ ጥላ ይሆናል። / A seed planted today becomes tomorrow’s shelter."*
  - Historical drought scenes feature a distinct, warm desaturated grade to distinguish the period setting from modern documentary reality.
- **In the file:**
  - The rendered MP4 container metadata (`final.mp4` and `final_9x16.mp4`) embeds title, credit tags, and automated creation comments explicitly declaring AI generation.
  - *Provenance notes:* Gemini TTS speech carries an inaudible Google SynthID digital watermark; however, subsequent multi-track mixing, loudness normalization (`loudnorm`), ducking, and AAC audio compression may alter the waveform. The visible on-screen title cards and metadata container tags therefore serve as the authoritative provenance.
- **On platforms:**
  - **Instagram Reels:** Set "AI-generated content" label to **ON**.
  - All social post descriptions include explicit disclosure that visuals, motion, and synthetic voiceover were generated using AI toolchains.
