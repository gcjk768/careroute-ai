'use client'

// Patient (end-user) app — the whole main experience at `/`.
import PatientLayout from '@/components/PatientLayout'
import PatientTriage from '@/components/PatientTriage'

export default function Page() {
  return (
    <PatientLayout>
      <PatientTriage />
    </PatientLayout>
  )
}
