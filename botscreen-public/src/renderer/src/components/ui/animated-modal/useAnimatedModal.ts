import { inject } from "vue";
import { ANIMATED_MODAL_KEY, type AnimatedModalContext } from "./AnimatedModalContext";

export function useAnimatedModal(): AnimatedModalContext {
  const modal = inject(ANIMATED_MODAL_KEY, null);
  if (!modal) {
    throw new Error("useAnimatedModal must be used within <AnimatedModal>");
  }
  return modal;
}
