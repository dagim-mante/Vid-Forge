# DESIGN — የመጨረሻው ዘር / The Last Seed

Written before any image, clip or music was generated (25 Nov 2026). Test notes are added below as they happen.

---

## Brief

| Field | Description |
|---|---|
| **Who watches** | Ethiopian youth and diaspora families; global audiences interested in African folktales, resilience, and environmental stewardship. |
| **Where** | On a phone, mostly vertical (Telegram, TikTok, YouTube Shorts, Instagram Reels); horizontal on YouTube. Often with sound low, so subtitles must carry the meaning. |
| **What they should feel / understand at the end** | True hope is not passive wishing; it is an active sacrifice planted today so future generations can find shelter. Small, patient actions outlive drought and despair. |
| **Define (one sentence)** | How might we inspire young viewers to choose patience and quiet generational action over despair when facing collective hardship? |
| **Constraints** | 60–90 s (target ~85 s), Amharic narration + English subtitles, zero budget (free tiers: Gemini, Cloudflare Workers AI, Kaggle GPU), ~1.5 days, synthetic AI figures avoiding human uncanny-valley artifacts. |

---

## Why this story matters to me

I love the title "The Last Seed" (*የመጨረሻው ዘር*) because it touches something fundamental in Ethiopian culture: the quiet endurance (*መጽናት*) of farmers who have lived with the land for millennia. In the highlands, a seed is never just food; it is a sacred bridge between generations. When scarcity strikes, the instinct is to consume whatever remains today. Alemu's choice to bury the last grain into parched soil is an act of defiance through faith. Telling this story through AI cinematic animation allows us to celebrate traditional values—patience, eldership, and environmental stewardship—while proving that modern generative media can honor authentic cultural depth.

---

## Story in five sentences

1. Severe drought parches an isolated Ethiopian highland village, withering the river and driving exhausted villagers to the brink of despair.
2. In his empty storehouse, an elderly farmer named Alemu discovers a single surviving indigenous seed.
3. When worried neighbors urge him to eat it to survive today, Alemu calmly asks what tomorrow will bring if nothing is planted.
4. Alone in the cracked earth, Alemu buries his faith in the stubborn soil and tends it through long, silent days until the rains return.
5. Decades later, that solitary seed has grown into a towering shade tree laden with fruit, sheltering and feeding the next generation.

---

## Design decisions

- **Visual progression (Ochre to Emerald):** Scenes 01–04 use a desaturated ochre and burnt-sienna grade with harsh, dusty highland sunlight. Scenes 05–06 transition into cool slate grey, deep rain-darkened umber, and soft morning gold. Scenes 07–08 bloom into rich emerald greens, golden canopy sunbeams, and vibrant terraced crops.
- **Authentic material culture & modesty:** All figures wear traditional hand-spun cotton garments (*shamma* ሻማ, *gabi* ጋቢ). Alemu's aged appearance carries dignity rather than decrepitude. Tools are simple wooden implements (*መቆፈሪያ*), storage vessels are unglazed clay and woven grass (*ጎተራ*, *ሰፌድ*), and architecture is strictly circular thatched stone-and-mud *tukuls* (ጎጆ ቤቶች).
- **No fantasy glow or magic morphing:** The seed is a tactile, matte-brown indigenous highland grain. When it grows, we match-cut across seasons rather than displaying unrealistic digital growth animations, keeping the tone rooted in magical realism and folklore.
- **Pacing governed by elder oral cadence:** Narration timing dictates clip lengths ($\text{shot duration} = \max(\text{planned length}, \text{narration} + \text{breathing room})$). The Amharic delivery follows the steady, warm cadence of an elder storytelling by an evening hearth (*እሳት ዳር*).
- **Centered composition for dual-format delivery:** Key actions, hands, and characters are kept within the central 60% safe zone of the 16:9 landscape master, ensuring the 9:16 vertical crop preserves narrative clarity without requiring pan-and-scan reframing.

---

## Shot list and reasons (mirrors storyboard.json)

| Shot | Era | What we see | Why it exists |
|---|---|---|---|
| **shot_01** | Drought | Aerial push-in over isolated highland village and cracked terraced fields | Establish scale, isolation, environmental drought, and solemn reverent tone |
| **shot_02** | Drought | Close-up of cracked riverbed stones and parched ground under harsh sun | Anchor the poetic metaphor: *"Hope dried up like the river"* |
| **shot_03** | Discovery | Alemu inside dim mud-plastered storehouse checking empty woven baskets | Introduce Alemu's quiet dignity amidst scarcity and loss |
| **shot_04** | Discovery | Extreme close-up of weathered hands unfolding cloth to reveal a dark seed | Reveal the central physical and spiritual anchor of the film |
| **shot_05** | Doubt | Four weathered villagers in dusty fields holding an empty clay water pot | Humanize collective fear and exhaustion; villagers are desperate, not villains |
| **shot_06** | Conviction | Close-up of Alemu's calm, observant face with etched lines and steady eyes | The turning point: Alemu's conviction (*"What will grow tomorrow?"*) |
| **shot_07** | Planting | Static wide of Alemu kneeling alone in cracked earth with a wooden digger | The solitary act of faith; burying hope into the dust |
| **shot_08** | Waiting | Time-lapse lighting shift from harsh noon to amber dusk over dry soil | Convey the weight of patience and the passage of silent, faithful time |
| **shot_09** | Renewal | Macro shot of a tiny vibrant green shoot emerging from damp dark earth | Life returns; the turning of fortunes rewarded by nature |
| **shot_10** | Legacy | Slow tilt-up from lush ground crops to a sprawling leafy highland canopy | Fulfill the promise: decades of growth and communal abundance |
| **shot_11** | Handover | Aged Alemu sitting on a wooden stool sharing fruit with a braided girl | Generational handover; thematic resolution (*"A great shelter begins with a single seed"*) |

---

## Viewer comments on the stills (received 26 Nov 2026)

| Comment | Change (storyboard prompts) |
|---|---|
| **User 1 (Culture consultant):** "Alemu's wrap in shot 03 looked too uniform and modern, like machine-woven wool." | **shot_03 & shot_04:** Prompt revised to explicitly specify *"hand-spun coarse cotton shamma with subtle raw thread irregularities and earth-stained edges"*. |
| **User 2 (Visual designer):** "The dry plot in shot 08 had small green tufts in the background, which contradicted the line that 'nothing sprouted'." | **shot_08:** Added negative prompt constraint *"green grass, sprouts, weeds, moisture"* and set prompt to *"100% dry ochre dirt with no plant growth"*. |
| **User 3 (Viewer):** "The seed in shot 04 looked too shiny, almost like a metallic bead." | **shot_04:** Changed *"smooth seed"* $\rightarrow$ *"single dark-brown textured, matte indigenous highland seed with organic grain surface"*. |
| **Viewer comment:** "The closing title card felt abrupt without an explicit moral takeaway." | **End card:** Formatted with Amharic proverb: *"ዛሬ የምትዘራው ዘር፣ የነገ ትውልድ ጥላ ይሆናል።"* (*A seed planted today becomes tomorrow's shelter*), followed by AI creation notice. |

> **Prompt lesson:** Generic adjectives like "traditional seed" led models to insert polished gem-like items. Describing physical surface attributes ("matte, coarse texture, organic irregularities, dark umber grain") yielded realistic highland seeds.

---

## User test 1 — radio cut (narration + temp music on black)

*Date / where:* 26 Nov 2026; headphone listening session with 3 participants (Addis Ababa & Diaspora).

| Person (role) | What they said / where they reacted | What we change |
|---|---|---|
| **First listener (student)** | *"The pauses between the lines felt a little too long in the beginning, like the narrator stopped reading."* | Reduced max silence padding between lines from 0.8s to 0.4s in `timing.json`; tightened overall radio cut from 94s to 84.9s. |
| **Second listener (Amharic speaker)** | *"In line 06, Alemu's dialogue was spoken too casually. He is answering the village doubt, so it needs weight."* | Updated Gemini TTS vocal prompt to specify `voice_style: "calm, warm, unhurried, elder storyteller's cadence"`; re-synthesized line 06. |
| **Third listener (English speaker)** | *"The masinko drone in scene 03 masked the lower registers of the Amharic consonants."* | Automated ducking adjusted to drop music by an additional $-3\text{ dB}$ during lines 05 and 06. |

**Insights:** Amharic consonants carry critical phonemic meaning; background instrumental levels must duck aggressively beneath speech. The tightened speech timeline kept the total film strictly under the 90-second constraint.

---

## User test 2 — near-final cut

*Date / where:* 26 Nov 2026; near-final cut (Ken Burns shots 02 & 06, Wan shots, burned-in subtitles) viewed on mobile phones (Telegram & TikTok preview) and desktop (16:9). · *People:* 3 viewers (2 fluent Amharic speakers, 1 non-Amharic speaker relying on subtitles).

| Person (role) | What they said / where they reacted | What we change |
|---|---|---|
| **Friend who speaks Amharic (Phone viewer)** | *"The camera move in shot 02 felt a bit fast and shaky for such a calm, sad opening."* | **Changed:** Ken Burns motion travel reduced from 16% linear zoom to 4% eased motion (`zoompan` smooth filter); camera move is now calm and contemplative. |
| **Friend (reads English subtitles)** | *"The yellow subtitles looked great on the dark soil, but in the final green tree scene they got slightly lost in the bright leaves."* | **Changed:** Subtitle styling updated with a semi-transparent black backing box (`BorderStyle=3`) and a crisp outline to guarantee legibility across both drought and lush green scenes. |
| **Friend who understands Amharic (parent)** | *"The transition from the sprout to the mature tree felt emotionally sudden. Seeing the young girl sitting with Alemu made me understand decades had passed, but maybe a gentle dissolve would help."* | **Changed:** Replaced hard cut with a 0.5-second crossfade transition between shot 09 and shot 10 (`--transition crossfade`); allows the visual passage of time to breathe. |

**Insights:**
- The 4% eased camera movement on Ken Burns still images gives an elegant documentary stillness that respects the solemn gravity of the drought.
- The 0.5s crossfade between sprout and mature tree successfully bridges the generational time-jump without confusing viewers.
- English subtitle legibility on vertical mobile video requires contrasting borders to stay readable against high-contrast green foliage.
