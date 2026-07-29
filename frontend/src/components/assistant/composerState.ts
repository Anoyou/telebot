export type ComposerEnterState = {
  key: string;
  shiftKey: boolean;
  nativeComposing: boolean;
  compositionActive: boolean;
  suppressAfterComposition: boolean;
};

export function composerEnterAction(state: ComposerEnterState): "ignore" | "suppress" | "submit" {
  if (state.key !== "Enter" || state.shiftKey) return "ignore";
  if (state.nativeComposing || state.compositionActive) return "ignore";
  if (state.suppressAfterComposition) return "suppress";
  return "submit";
}
