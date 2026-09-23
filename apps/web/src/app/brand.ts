/**
 * Product identity used by the shell, sign-in and document titles. The
 * company fork replaces this file and drops its own logo into public/.
 */
export const BRAND = {
  name: "Cairn",
  /** Path to a logo image under public/; null renders the stone mark. */
  logo: null as string | null,
  /** Path to a favicon under public/; null keeps index.html's default. */
  favicon: null as string | null,
};
