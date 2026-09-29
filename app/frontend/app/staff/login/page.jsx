// Mock staff sign-in — deliberately OUTSIDE the (portal) route group so it is not
// behind the auth gate and shows no staff chrome.
import StaffLogin from '@/components/StaffLogin'

export default function Page() {
  return <StaffLogin />
}
