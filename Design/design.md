# Joint Minds Knowledge Network (JMKN Tech) - Design System

This document outlines the complete design system and visual guidelines for **JMKN (Joint Minds Knowledge Network) Tech**. 

**Official Brand Domain**: [jmkn.tech](https://jmkn.tech)

Our design system is built to balance research-grade technological sophistication (DeepMind) with high-end, clean consumer product aesthetics (Apple, Ultrahuman) and reliable marketplace execution (Bazaar). The design language communicates premium expertise, technical precision, and measurable business impact, avoiding "cyberpunk" or "edgy" developer clichés.

---

## 1. Design Principles

* **UX in Our Veins**: Layouts must prioritize clarity and low cognitive load. Interfaces should feel lightweight, responsive, and intuitive.
* **Play to Individuality**: Avoid cookie-cutter templates. The UI should reflect the bespoke nature of our client solutions.
* **Impact Over Checklist**: Every visual element must serve a purpose. We use color, size, and layout to highlight outcomes (metrics, statistics, business results) rather than static technical specs.
* **Anodized Precision**: We build with sharp, clean typography, thin line-weights, and generous whitespace, mirroring high-end hardware interfaces.

---

## 2. Color System (Option A: Sky Cyan & Gold)

To maintain a professional, clean corporate look, the brand supports a primary **Light Mode** (default) representing clean space, silver, and high-end aluminum, alongside a **Dark Mode** (toggle) representing premium obsidian.

### Theme Values

| Token Name | Light Mode (Default) | Dark Mode | Usage |
| :--- | :--- | :--- | :--- |
| `bg-primary` | `#F5F5F7` (Apple Silver) | `#09090C` (Obsidian) | Primary page background |
| `bg-card` | `rgba(255, 255, 255, 0.8)`| `rgba(18, 18, 26, 0.7)` | Glassmorphic cards |
| `bg-well` | `#FFFFFF` (Pure White) | `#12121A` (Charcoal) | Section containers, wells |
| `gold-primary` | `#FFC107` (MacBook Gold) | `#FFC107` (Bright Gold) | Primary CTA buttons, key actions |
| `violet-primary`| `#92E2EC` (Sky Cyan) | `#92E2EC` (Sky Cyan) | Secondary accent, AI nodes, subhero focus |
| `border` | `#D2D2D7` (Apple Gray) | `#21222D` (Slate Gray) | Fine border lines, dividers |
| `text-primary` | `#1D1D1F` (Obsidian Black) | `#FFFFFF` (Space White) | Main headings, primary copy |
| `text-secondary`| `#515154` (Muted Charcoal) | `#FFF8E7` (Champagne White)| Body text, descriptive copy |
| `text-muted` | `#86868B` (Apple Gray) | `#8E8E93` (Space Silver) | Metadata, captions, labels |

---

## 3. Typography & Tone of Voice

Based on Apple, DeepMind, and Ultrahuman typography, we use **Inter** as our core typeface. We **avoid heavy bold weights** (700, 800, 900) which look aggressive. Instead, we use **Light (300)**, **Regular (400)**, and **Medium (500)** weights at larger font sizes for an elegant, intellectual, and premium technical feel. **Semibold (600)** is permitted only for small UI labels, numeric accents, and metadata — never for large display headings.

### Typography Stack
* **Primary (Headings & UI)**: `Inter`, `-apple-system`, `BlinkMacSystemFont`, `sans-serif`
* **Secondary (Code & Technical Data)**: `JetBrains Mono`, `SF Mono`, `Fira Code`, `monospace`

### Font Weights & Styles
* **Display Headings (H1)**: `font-size: 56px`, `font-weight: 500` (Medium), `letter-spacing: -0.02em`
* **H2 (Section Title)**: `font-size: 32px`, `font-weight: 500` (Medium), `letter-spacing: -0.01em`
* **H3 (Card Title)**: `font-size: 20px`, `font-weight: 500` (Medium), `letter-spacing: -0.01em`
* **Body Text**: `font-size: 16px`, `font-weight: 400` (Regular), `line-height: 1.6`
* **Captions / Small**: `font-size: 13px`, `font-weight: 500` (Medium), `letter-spacing: 0.05em`

---

## 4. Zero-Gravity Reactivity Specification

To make our digital product interfaces feel alive, we implement a **Zero-Gravity Reactivity system**. Inspired by Google's physics experiments, elements react organically to mouse cursors and viewport scrolls, moving with fluid spring physics.

### A. Mouse-Reactive Node Fields (AI Graphic)
Visual network elements (such as the custom Knowledge Brain) must have dynamic physics:
* **Passive Drift**: Nodes float on slow, independent sin/cos wave pathways to simulate a weightless environment.
* **Attraction/Repulsion**: Cursors exert a subtle repelling field. When the mouse cursor approaches a node, the node drifts away smoothly, and the connecting paths bend.
* **Spring Return**: Once the cursor leaves the field, the nodes slowly glide back to their home coordinate with soft dampening.

### B. 3D Parallax Tilt (Cards & Components)
Interactive cards (such as capability cards) use a 3D tilt-on-hover effect:
* **Logic**: On mouse move inside the card boundary, calculate the cursor offset from the card's center.
* **Execution**: Apply a subtle 3D rotation (`transform: rotateX(...) rotateY(...)`) maxing out at `6 degrees`.
* **Reflective Light (Shine)**: A light radial gradient follow-light should trace the cursor coordinates across the card surface, acting as a glossy reflection.

---

## 5. CSS & JS Reactivity Tokens

```css
/* JMKN Tech Design Tokens */

/* --- LIGHT THEME (Default) --- */
:root {
  --jmkn-bg-primary: #F5F5F7;
  --jmkn-bg-card: rgba(255, 255, 255, 0.8);
  --jmkn-bg-well: #FFFFFF;
  
  --jmkn-gold-primary: #FFC107;
  --jmkn-gold-gradient: linear-gradient(135deg, #FFE082 0%, #FFC107 40%, #FF8F00 100%);
  
  --jmkn-violet-primary: #92E2EC;
  --jmkn-violet-gradient: linear-gradient(135deg, #BDEDF3 0%, #92E2EC 100%);
  --jmkn-violet-glow: rgba(146, 226, 236, 0.1);

  --jmkn-border: #D2D2D7;

  --jmkn-text-primary: #1D1D1F;
  --jmkn-text-secondary: #515154;
  --jmkn-text-muted: #86868B;

  --jmkn-shadow: 0 10px 30px rgba(0, 0, 0, 0.04);
  --jmkn-shadow-hover: 0 20px 40px rgba(0, 0, 0, 0.08), 0 0 20px rgba(146, 226, 236, 0.08);

  --jmkn-font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
  --jmkn-font-mono: 'SF Mono', 'Fira Code', monospace;
  --jmkn-radius-large: 20px;
  --jmkn-radius-small: 8px;
}

/* --- DARK THEME --- */
[data-theme="dark"] {
  --jmkn-bg-primary: #09090C;
  --jmkn-bg-card: rgba(18, 18, 26, 0.7);
  --jmkn-bg-well: #12121A;
  
  --jmkn-gold-primary: #FFC107;
  --jmkn-gold-gradient: linear-gradient(135deg, #FFE082 0%, #FFC107 40%, #FF8F00 100%);
  
  --jmkn-violet-primary: #92E2EC;
  --jmkn-violet-gradient: linear-gradient(135deg, #BDEDF3 0%, #92E2EC 100%);
  --jmkn-violet-glow: rgba(146, 226, 236, 0.2);

  --jmkn-border: #21222D;
  
  --jmkn-text-primary: #FFFFFF;
  --jmkn-text-secondary: #FFF8E7;
  --jmkn-text-muted: #8E8E93;
  
  --jmkn-shadow: none;
  --jmkn-shadow-hover: 0 20px 40px rgba(0, 0, 0, 0.4), 0 0 25px var(--jmkn-violet-glow);
}
```

---

## 6. Brand Logo Assets

We have designed a custom logo mark representing a staggered 2x2 keyboard keycap cluster containing the keys **J, K, N, M** (Joint Minds Knowledge Network). This keyboard metaphor reflects our focus on developer intelligence, precision engineering, and interactive tools.

The brand guidelines split the logo assets into separate variants for dark mode and light mode, ensuring optimal contrast and aesthetics on both background styles:
* **Dark Mode Variants** (Original): Designed for obsidian/dark backgrounds (`#09090C`). Features dark keycaps, glowing gold legends, and a dark border bezel.
* **Light Mode Variants** (New): Designed for silver/white backgrounds (`#F5F5F7` / `#FFFFFF`). Features dark keycaps (matching dark mode keycap colors), light legends, and a subtle light bezel frame.

### Asset File Inventory

| Category | Dark Mode (Primary/Original) | Light Mode (Complementary) | Description |
| :--- | :--- | :--- | :--- |
| **Static SVG** | [logo_staggered_dark.svg](./logo_staggered_dark.svg) | [logo_staggered_light.svg](./logo_staggered_light.svg) | Clean vector mark with transparent background and bezel wrapper. |
| **Animated GIF** | [logo_animated_dark.gif](./logo_animated_dark.gif) | [logo_animated_light.gif](./logo_animated_light.gif) | Transparent background high-res GIF showing typing animations for non-vector usage. |


