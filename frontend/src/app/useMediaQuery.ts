import { useEffect, useState } from "react";

/**
 * Tracks a CSS media query in React state.
 *
 * The first render already reports the real match, so the desktop shell never
 * flashes an empty layout while a breakpoint observer warms up.
 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() =>
    typeof window === "undefined" ? false : window.matchMedia(query).matches,
  );

  useEffect(() => {
    if (typeof window === "undefined") return;
    const media = window.matchMedia(query);
    const update = () => setMatches(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [query]);

  return matches;
}
