'use client'

// Gated staff-portal shell: bounce to /staff/login without a local session, or
// when the server says its signed session cookie is missing/expired. This gate
// is UX only — the escalation proxy enforces the cookie on every request.
import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { getStaff, staffSession } from '@/lib/auth'
import StaffLayout from '@/components/StaffLayout'

export default function PortalLayout({ children }) {
  const router = useRouter()
  const [authed, setAuthed] = useState(false)

  useEffect(() => {
    if (!getStaff()) {
      router.replace('/staff/login')
      return
    }
    staffSession().then((s) => {
      if (s?.required && !s.authenticated) router.replace('/staff/login')
      else setAuthed(true)
    })
  }, [router])

  // Render nothing until the session check passes (also avoids an SSR/client
  // hydration mismatch, since localStorage is only available in the browser).
  if (!authed) return null

  return <StaffLayout>{children}</StaffLayout>
}
