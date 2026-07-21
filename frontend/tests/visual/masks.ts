export const dynamicMaskSelectors = [
  "[data-visual-mask]",
];

export async function applyVisualMasks(page: { addStyleTag: (options: { content: string }) => Promise<unknown> }) {
  await page.addStyleTag({
    content: `${dynamicMaskSelectors.join(",")} { visibility: hidden !important; } * { animation-duration: 0s !important; transition-duration: 0s !important; }`,
  });
}
