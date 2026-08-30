import { Route, Routes } from "react-router-dom";
import Landing from "./pages/Landing.jsx";
import Verify from "./pages/Verify.jsx";
import Admin from "./pages/Admin.jsx";
import Review from "./pages/Review.jsx";

// Route paths match the old static-file routes exactly (app.py serves this
// build's index.html for all four, react-router picks the page client-side)
// so no bookmarked/linked URL changes: / , /verify-page , /admin , /review.
export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Landing />} />
      <Route path="/verify-page" element={<Verify />} />
      <Route path="/admin" element={<Admin />} />
      <Route path="/review" element={<Review />} />
    </Routes>
  );
}
