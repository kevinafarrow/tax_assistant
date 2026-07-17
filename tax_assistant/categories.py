"""IRS Schedule C Part II expense categories.

The dict maps the category name (what the model classifies into, and what the
ledger stores) to the directory prefix used on disk. Prefixes carry the
Schedule C line number so the folder tree maps directly onto the form.
"""

SCHEDULE_C_CATEGORIES: dict[str, str] = {
    "Advertising": "08-Advertising",
    "Car and truck expenses": "09-Car-and-Truck",
    "Commissions and fees": "10-Commissions-and-Fees",
    "Contract labor": "11-Contract-Labor",
    "Depreciation": "13-Depreciation",
    "Employee benefit programs": "14-Employee-Benefits",
    "Insurance": "15-Insurance",
    "Interest": "16-Interest",
    "Legal and professional services": "17-Legal-and-Professional",
    "Office expense": "18-Office-Expense",
    "Rent or lease": "20-Rent-or-Lease",
    "Repairs and maintenance": "21-Repairs-and-Maintenance",
    "Supplies": "22-Supplies",
    "Taxes and licenses": "23-Taxes-and-Licenses",
    "Travel": "24a-Travel",
    "Meals": "24b-Meals",
    "Utilities": "25-Utilities",
    "Wages": "26-Wages",
    "Other expenses": "27a-Other",
}

CATEGORY_NAMES = tuple(SCHEDULE_C_CATEGORIES.keys())


def category_dir(category: str) -> str:
    return SCHEDULE_C_CATEGORIES[category]
