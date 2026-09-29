// Root layout — the single <html>/<body> shell for every route. The three brand
// typefaces are loaded from Google Fonts via <link> (browser-side, matching the
// original app) rather than next/font, which fetches at build time and is
// unreliable on constrained networks. Route-specific chrome (patient bar, staff
// rail) lives in the individual route trees, not here.
import './globals.css'

export const metadata = {
  title: 'CareRoute AI — Clinical Triage Console',
  description:
    'CareRoute AI — a safety-gated, bias-audited multi-agent healthcare triage assistant.',
}

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link
          href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600;9..144,700&family=Hanken+Grotesk:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap"
          rel="stylesheet"
        />
      </head>
      <body>
        {/* App content sits above the fixed grain/gradient layers painted on <body>. */}
        <div className="app-root">{children}</div>
      </body>
    </html>
  )
}
