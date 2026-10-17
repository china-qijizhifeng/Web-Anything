# USWDS two-page reconstruction demo

Source: https://designsystem.digital.gov/components/overview/
Target viewport: desktop 1440 x 900.

## Scope

- Reconstruct the public `components/overview/` page and `components/accordion/` page as two local routes. Other source routes are outside this demo's acceptance scope.
- Keep the page structure, type scale, spacing, colors, navigation, component grid, accordion examples, and responsive behavior visibly close to the source at the target viewport.
- On the overview page, the component filter must work for `accordion` and for an unmatched term; clearing it must restore the list. Clicking the Accordion component must open the local accordion route.
- On the accordion page, the example panels must open and close with pointer and keyboard controls, update `aria-expanded`, and preserve readable content. A visible link must return to the local overview page.
- Links within these two routes must be local and functional. For source links beyond scope, use a clear non-deceptive external link to the original documentation instead of a placeholder.
- No login, account workflow, backend mutation, or broad crawl is needed; this is public reference documentation.

## Rights and runtime

- The USWDS website repository declares its original content public domain/CC0. Only use source content from these two pages and public-domain/CC0 first-party assets whose status is clear. Do not copy government seals, agency logos, third-party photos, bundled commercial fonts, or material with unclear rights. Plain text site naming is acceptable for navigation.
- Keep the reconstructed pages self-contained and operable without calling the source at runtime. Use local CSS and system fonts.
- Browser console errors, missing local resources, dead in-scope controls, and horizontal overflow fail acceptance.

## Evidence

- Capture usable source and local screenshots for both routes at 1440 x 900.
- Verify the filter, route navigation, accordion open/close, keyboard action, and accessible expanded state in a real browser.
- Run the repository's independent verifier and record the candidate hash and its result. Report incomplete or blocked states honestly.
