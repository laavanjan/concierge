"""Generate the sample policy PDF (data/policies/house-rules-and-safety.pdf).

The RAG pipeline must handle real PDFs, so one of the policy documents ships as
a two-page PDF and answers cite its page numbers. Requires `fpdf2` (dev extra).
"""

from __future__ import annotations

from pathlib import Path

PAGES = [
    (
        "House Rules and Safety",
        [
            ("Visitors", "Registered guests may receive visitors in public areas until 22:00. Overnight visitors must be "
             "registered at the front desk and may change the room rate."),
            ("Pool rules", "The outdoor pool is open 07:00 to 21:00 and has no lifeguard. Children under 14 must be "
             "accompanied by an adult. Glassware is not allowed on the pool deck."),
            ("Valuables", "Every room has an in-room safe large enough for a laptop. The hotel is not responsible for "
             "valuables left outside the safe."),
        ],
    ),
    (
        "Safety and Lost Property",
        [
            ("Fire safety", "Fire exits are marked on the back of every room door. In a fire alarm, leave by the stairs, "
             "never the lift, and gather at the assembly point in the front car park."),
            ("Lost property", "Lost property is kept for 90 days. We can post items back for the cost of shipping; email "
             "frontdesk@aurora-bay.example with a description."),
            ("Damage", "Guests are responsible for damage beyond normal wear. Charges are agreed with the duty manager "
             "before they are taken from the incidentals deposit."),
        ],
    ),
]


def main(out: Path | None = None) -> Path:
    from fpdf import FPDF

    out = out or Path(__file__).resolve().parent.parent / "data" / "policies" / "house-rules-and-safety.pdf"
    pdf = FPDF()
    pdf.set_title("House Rules and Safety")
    for title, sections in PAGES:
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 18)
        pdf.cell(0, 12, title, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(0, 6, "Aurora Bay Hotel (fictional demo property). Version 2026-11.", new_x="LMARGIN", new_y="NEXT")
        for heading, body in sections:
            pdf.ln(4)
            pdf.set_font("Helvetica", "B", 13)
            pdf.cell(0, 8, heading, new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", "", 11)
            pdf.multi_cell(0, 6, body)
    pdf.output(str(out))
    return out


if __name__ == "__main__":
    print(main())
