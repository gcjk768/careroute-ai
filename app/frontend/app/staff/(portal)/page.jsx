'use client'

// /staff → redirect to the default staff surface (pipeline), staying inside the
// gated portal layout.
import { useEffect } from 'react'
import { useRouter } from 'next/navigation'

export default function StaffIndex() {
  const router = useRouter()
  useEffect(() => {
    router.replace('/staff/pipeline')
  }, [router])
  return null
}
