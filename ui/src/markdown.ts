/**
 * What the console's two Markdown views share: the documentation (pages/Docs.tsx) and a machine's
 * Markdown files (pages/MachineFiles.tsx). Raw HTML is always shown as text, so nothing a page or a
 * file holds runs in the console.
 */

export const escapeHtml = (text: string) => text.replace(/[&<>"']/g, (char) => `&#${char.charCodeAt(0)};`);

/** A heading's anchor, the way GitHub makes them, so links written for the repository work here too. */
export function slug(text: string): string {
  return text
    .trim()
    .toLowerCase()
    .replace(/<[^>]+>/g, "")
    .replace(/[`*_]/g, "")
    .replace(/[^\p{L}\p{N}\- ]/gu, "")
    .replace(/ /g, "-");
}

/** A Mermaid code block, kept as its source until renderDiagrams draws it. */
export const mermaidBlock = (text: string) => `<div class="mermaid-diagram"><pre class="mermaid-source"><code>${escapeHtml(text)}</code></pre></div>\n`;

let mermaidLoader: Promise<typeof import("mermaid").default> | null = null;
let diagramCount = 0;

/** Draws the Mermaid diagrams inside root (sequence diagrams and others), in the console's light or dark theme.
 * mermaid.js is large, so it's only loaded when there's a diagram. */
export async function renderDiagrams(root: HTMLElement): Promise<void> {
  const blocks = [...root.querySelectorAll<HTMLElement>(".mermaid-diagram")];
  if (!blocks.length) return;
  mermaidLoader ??= import("mermaid").then((module) => module.default);
  let mermaid: Awaited<typeof mermaidLoader>;
  try {
    mermaid = await mermaidLoader;
  } catch (error) {
    // Most often a console from before the server's last update, whose mermaid.js is gone (main.tsx
    // reloads for that). Try again next time, and say why on each diagram instead of leaving its source.
    mermaidLoader = null;
    for (const block of blocks) {
      const source = block.dataset.source ?? block.querySelector(".mermaid-source")?.textContent ?? "";
      block.dataset.source = source;
      block.classList.add("mermaid-failed");
      block.innerHTML = `<pre class="mermaid-source"><code>${escapeHtml(source)}</code></pre><p class="bad">The diagram drawer didn't load (${escapeHtml(String((error as Error)?.message ?? error))}). Reload the page.</p>`;
    }
    return;
  }
  const dark = document.documentElement.dataset.theme === "dark";
  mermaid.initialize({
    startOnLoad: false,
    securityLevel: "strict",
    theme: dark ? "dark" : "default",
    fontFamily: "Inter Variable, Inter, sans-serif",
    flowchart: { curve: "basis", wrappingWidth: 260, padding: 14 },
    sequence: { mirrorActors: false, wrap: true, width: 170 },
  });
  for (const block of blocks) {
    const source = block.dataset.source ?? block.querySelector(".mermaid-source")?.textContent ?? "";
    block.dataset.source = source;
    try {
      const { svg } = await mermaid.render(`mermaid-${++diagramCount}`, source);
      block.innerHTML = svg;
      block.classList.remove("mermaid-failed");
    } catch (error) {
      // Show the source as it is, and why it couldn't be drawn.
      block.classList.add("mermaid-failed");
      block.innerHTML = `<pre class="mermaid-source"><code>${escapeHtml(source)}</code></pre><p class="bad">${escapeHtml(String((error as Error)?.message ?? error))}</p>`;
    }
  }
}

/** Draws root's diagrams now, and again whenever the console's theme changes. Returns the way to stop. */
export function watchDiagrams(root: HTMLElement): () => void {
  void renderDiagrams(root);
  const observer = new MutationObserver(() => void renderDiagrams(root));
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  return () => observer.disconnect();
}
