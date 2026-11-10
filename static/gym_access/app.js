const STORAGE_KEY = "salonMaxAccessDemo.v4";
const CHECKOUT_RETURN_KEY = "salonMaxAccessPendingCheckout.v1";

const defaultClasses = [
  { id: "gym_floor", name: "Gym floor", description: "General gym floor access during staffed opening hours.", openAccess: true },
  { id: "fb_power", name: "FB Power", description: "Functional strength and conditioning class.", sessions: [{ day: 1, time: "06:00", durationMinutes: 60 }, { day: 1, time: "09:30", durationMinutes: 60 }, { day: 1, time: "18:30", durationMinutes: 60 }] },
  { id: "functional_fit", name: "Functional Fit", description: "High-energy functional fitness session.", sessions: [{ day: 2, time: "06:00", durationMinutes: 60 }, { day: 3, time: "09:30", durationMinutes: 60 }] },
  { id: "strength", name: "Strength", description: "Strength-focused training session.", sessions: [{ day: 3, time: "06:00", durationMinutes: 60 }, { day: 5, time: "09:30", durationMinutes: 60 }, { day: 3, time: "18:30", durationMinutes: 60 }] },
  { id: "full_body", name: "Full Body", description: "Full-body training session for all levels.", sessions: [{ day: 4, time: "06:00", durationMinutes: 60 }, { day: 4, time: "09:30", durationMinutes: 60 }] },
  { id: "boxfit", name: "Boxfit", description: "Boxing-inspired fitness class.", sessions: [{ day: 2, time: "09:30", durationMinutes: 60 }, { day: 2, time: "18:30", durationMinutes: 60 }] },
  { id: "pilates", name: "Pilates", description: "Low-impact strength, mobility, and core session.", sessions: [{ day: 1, time: "10:30", durationMinutes: 60 }, { day: 4, time: "18:30", durationMinutes: 60 }] },
  { id: "saturday_sweat", name: "Saturday Sweat", description: "Weekend group training session.", sessions: [{ day: 6, time: "09:30", durationMinutes: 60 }] }
];

const defaultState = {
  selectedTenantId: "tenant-peak",
  view: "customer",
  tenants: [
    {
      id: "tenant-peak",
      name: "KADO Fitness",
      location: "Manchester",
      addressLine1: "KADO Fitness Studio",
      addressLine2: "Unit 4, Mill Lane",
      postcode: "M1 4AB",
      contactName: "KADO Fitness Manager",
      contactEmail: "hello@kadofitness.example",
      contactPhone: "0161 000 1234",
      subscriptionStatus: "active",
      salonMaxFee: 100,
      freeTrialUntil: addDaysIso(14),
      billingDay: 1,
      adminNotes: "Demo KADO Fitness account.",
      stripeConnected: true,
      brand: {
        name: "KADO Fitness",
        tagline: "Women-only functional fitness with no booking and no contract.",
        heroImage: "/static/gym_access/assets/kado-timetable.jpg",
        accent: "neon"
      },
      classes: cloneDefaultClasses(),
      plans: [
        { id: "plan-month", name: "KADO Membership", price: 30, durationDays: 30, billing: "monthly", description: "No need to book. No contract. Women only.", entitlements: ["gym_floor", "fb_power", "functional_fit", "strength", "full_body", "boxfit", "pilates", "saturday_sweat"] },
        { id: "plan-class-pass", name: "Pay per class", price: 7, durationDays: 1, billing: "one-off", description: "Single class access for the selected day.", entitlements: ["fb_power", "functional_fit", "strength", "full_body", "boxfit", "pilates", "saturday_sweat"] },
        { id: "plan-fb-power", name: "FB Power monthly", price: 18, durationDays: 30, billing: "monthly", description: "FB Power classes only.", entitlements: ["fb_power"] },
        { id: "plan-boxfit", name: "Boxfit monthly", price: 18, durationDays: 30, billing: "monthly", description: "Boxfit classes only.", entitlements: ["boxfit"] },
        { id: "plan-pilates", name: "Pilates monthly", price: 18, durationDays: 30, billing: "monthly", description: "Pilates classes only.", entitlements: ["pilates"] },
        { id: "plan-strength", name: "Strength monthly", price: 18, durationDays: 30, billing: "monthly", description: "Strength classes only.", entitlements: ["strength"] }
      ],
      members: [
        {
          id: "mem-1",
          name: "Jamie Ellis",
          email: "jamie@example.com",
          phone: "07123 000111",
          marketingConsent: true,
          whatsappConsent: true,
          marketingSource: "Founder member",
          password: "password",
          planId: "plan-month",
          status: "active",
          credentialType: "PIN",
          credential: "2481",
          expiresAt: addDaysIso(18),
          createdAt: new Date().toISOString()
        },
        {
          id: "mem-2",
          name: "Priya Shah",
          email: "priya@example.com",
          phone: "07123 000222",
          marketingConsent: false,
          whatsappConsent: true,
          marketingSource: "Instagram",
          password: "password",
          planId: "plan-boxfit",
          status: "active",
          credentialType: "PIN",
          credential: "1357",
          expiresAt: addDaysIso(22),
          createdAt: new Date().toISOString()
        }
      ],
      accessLogs: []
    },
    {
      id: "tenant-axis",
      name: "Axis Strength Club",
      location: "Leeds",
      addressLine1: "Axis Strength Club",
      addressLine2: "42 Foundry Road",
      postcode: "LS1 8AA",
      contactName: "Axis Manager",
      contactEmail: "team@axisstrength.example",
      contactPhone: "0113 000 4567",
      subscriptionStatus: "past_due",
      salonMaxFee: 100,
      freeTrialUntil: "",
      billingDay: 1,
      adminNotes: "Payment method needs attention.",
      stripeConnected: false,
      classes: cloneDefaultClasses(),
      plans: [
        { id: "plan-axis-month", name: "Strength monthly", price: 45, durationDays: 30, billing: "monthly", entitlements: ["gym_floor", "hiit", "circuits"] },
        { id: "plan-axis-spin", name: "Spin monthly", price: 25, durationDays: 30, billing: "monthly", entitlements: ["spin"] },
        { id: "plan-axis-week", name: "Seven day trial", price: 12, durationDays: 7, billing: "one-off", entitlements: ["gym_floor"] }
      ],
      members: [],
      accessLogs: []
    }
  ],
  latestMemberId: null
};

let state = loadState();
const SURFACE = document.body.dataset.surface || "all";
const BUSINESS_ID = document.body.dataset.businessId || "biz_test-2";
const SURFACE_VIEWS = new Set(["customer", "reception", "staff", "owner"]);
if (SURFACE_VIEWS.has(SURFACE)) {
  state.view = SURFACE;
}

const els = {
  tenantSelect: document.querySelector("#tenantSelect"),
  pageTitle: document.querySelector("#pageTitle"),
  suspendedBanner: document.querySelector("#suspendedBanner"),
  systemDot: document.querySelector("#systemDot"),
  systemStatus: document.querySelector("#systemStatus"),
  systemHint: document.querySelector("#systemHint"),
  customerHero: document.querySelector("#customerHero"),
  publicBrandLabel: document.querySelector("#publicBrandLabel"),
  publicHeroTitle: document.querySelector("#publicHeroTitle"),
  publicHeroTagline: document.querySelector("#publicHeroTagline"),
  heroMembershipPrice: document.querySelector("#heroMembershipPrice"),
  heroClassPrice: document.querySelector("#heroClassPrice"),
  publicPlanCatalog: document.querySelector("#publicPlanCatalog"),
  signupForm: document.querySelector("#signupForm"),
  memberName: document.querySelector("#memberName"),
  memberEmail: document.querySelector("#memberEmail"),
  memberPassword: document.querySelector("#memberPassword"),
  payButton: document.querySelector("#payButton"),
  signupConfirmation: document.querySelector("#signupConfirmation"),
  latestMemberTitle: document.querySelector("#latestMemberTitle"),
  latestMemberCard: document.querySelector("#latestMemberCard"),
  packageChoiceList: document.querySelector("#packageChoiceList"),
  packagePurchaseMessage: document.querySelector("#packagePurchaseMessage"),
  checkoutBackButton: document.querySelector("#checkoutBackButton"),
  checkoutSummary: document.querySelector("#checkoutSummary"),
  checkoutForm: document.querySelector("#checkoutForm"),
  checkoutName: document.querySelector("#checkoutName"),
  accountEmail: document.querySelector("#accountEmail"),
  accountPassword: document.querySelector("#accountPassword"),
  memberLoginForm: document.querySelector("#memberLoginForm"),
  memberLoginButton: document.querySelector("#memberLoginButton"),
  memberSessionActions: document.querySelector("#memberSessionActions"),
  memberLogoutButton: document.querySelector("#memberLogoutButton"),
  memberTabs: document.querySelector("#memberTabs"),
  memberBuyPanel: document.querySelector("#memberBuyPanel"),
  memberBuyClassList: document.querySelector("#memberBuyClassList"),
  accountSummary: document.querySelector("#accountSummary"),
  kioskGymName: document.querySelector("#kioskGymName"),
  kioskVisitSelect: document.querySelector("#kioskVisitSelect"),
  kioskPinDisplay: document.querySelector("#kioskPinDisplay"),
  kioskSubmit: document.querySelector("#kioskSubmit"),
  kioskResult: document.querySelector("#kioskResult"),
  activeMembers: document.querySelector("#activeMembers"),
  monthlyRevenue: document.querySelector("#monthlyRevenue"),
  accessAttempts: document.querySelector("#accessAttempts"),
  attendanceCount: document.querySelector("#attendanceCount"),
  stripeStatus: document.querySelector("#stripeStatus"),
  connectStripe: document.querySelector("#connectStripe"),
  activeMemberList: document.querySelector("#activeMemberList"),
  expiredMemberList: document.querySelector("#expiredMemberList"),
  staffCredential: document.querySelector("#staffCredential"),
  staffVisitSelect: document.querySelector("#staffVisitSelect"),
  staffDoorCheck: document.querySelector("#staffDoorCheck"),
  staffDoorResult: document.querySelector("#staffDoorResult"),
  accessLog: document.querySelector("#accessLog"),
  attendanceAnalytics: document.querySelector("#attendanceAnalytics"),
  financeSummary: document.querySelector("#financeSummary"),
  classRevenueList: document.querySelector("#classRevenueList"),
  unpaidMemberList: document.querySelector("#unpaidMemberList"),
  paymentLedger: document.querySelector("#paymentLedger"),
  marketingStatusFilter: document.querySelector("#marketingStatusFilter"),
  marketingConsentFilter: document.querySelector("#marketingConsentFilter"),
  marketingPackageFilter: document.querySelector("#marketingPackageFilter"),
  marketingSearch: document.querySelector("#marketingSearch"),
  marketingOffer: document.querySelector("#marketingOffer"),
  marketingSubject: document.querySelector("#marketingSubject"),
  marketingBody: document.querySelector("#marketingBody"),
  marketingBuildCampaign: document.querySelector("#marketingBuildCampaign"),
  marketingCopyEmails: document.querySelector("#marketingCopyEmails"),
  marketingCopyMessage: document.querySelector("#marketingCopyMessage"),
  marketingCopyStatus: document.querySelector("#marketingCopyStatus"),
  marketingRecipientCount: document.querySelector("#marketingRecipientCount"),
  marketingPreview: document.querySelector("#marketingPreview"),
  classPackageForm: document.querySelector("#classPackageForm"),
  newClassName: document.querySelector("#newClassName"),
  newClassPrice: document.querySelector("#newClassPrice"),
  newClassDurationWeeks: document.querySelector("#newClassDurationWeeks"),
  newSessionTime: document.querySelector("#newSessionTime"),
  newSessionDuration: document.querySelector("#newSessionDuration"),
  addSessionSlot: document.querySelector("#addSessionSlot"),
  sessionPreview: document.querySelector("#sessionPreview"),
  pricingList: document.querySelector("#pricingList"),
  tenantCount: document.querySelector("#tenantCount"),
  ownerMrr: document.querySelector("#ownerMrr"),
  suspendedCount: document.querySelector("#suspendedCount"),
  totalMembers: document.querySelector("#totalMembers"),
  tenantList: document.querySelector("#tenantList"),
  addTenant: document.querySelector("#addTenant"),
  resetDemo: document.querySelector("#resetDemo")
};

let kioskPin = "";
let loggedInMemberId = null;
let pendingCheckout = null;
let pendingClassSessions = [];

document.querySelectorAll(".nav-button").forEach((button) => {
  button.addEventListener("click", () => {
    if (SURFACE !== "all") return;
    state.view = button.dataset.view;
    saveState();
    render();
  });
});

document.querySelectorAll("[data-step-target]").forEach((button) => {
  button.addEventListener("click", () => {
    if (button.dataset.loginEntry === "true") {
      startMemberLogin();
    }
    showCustomerStep(button.dataset.stepTarget);
  });
});

function showCustomerStep(step) {
  document.querySelectorAll(".signup-step").forEach((section) => {
    section.classList.toggle("is-active", section.dataset.step === step);
  });
}

els.tenantSelect.addEventListener("change", (event) => {
  state.selectedTenantId = event.target.value;
  state.latestMemberId = null;
  saveState();
  render();
});

els.signupForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const tenant = selectedTenant();

  if (tenant.subscriptionStatus === "suspended") {
    renderSignupMessage("Signup blocked. This gym is suspended by Salon Max.");
    return;
  }

  if (!tenant.stripeConnected) {
    renderSignupMessage("Signup blocked. Gym staff need to connect Stripe first.");
    return;
  }

  const member = {
    id: `mem-${Date.now()}`,
    name: els.memberName.value.trim(),
    email: els.memberEmail.value.trim(),
    phone: "",
    marketingConsent: false,
    whatsappConsent: false,
    marketingSource: "Website signup",
    password: els.memberPassword.value,
    planId: null,
    packageIds: [],
    status: "active",
    credentialType: "PIN",
    credential: createCredential("PIN"),
    expiresAt: new Date().toISOString(),
    createdAt: new Date().toISOString()
  };

  tenant.members.unshift(member);
  state.latestMemberId = member.id;
  els.accountEmail.value = member.email;
  els.accountPassword.value = member.password;
  loggedInMemberId = member.id;
  els.packagePurchaseMessage.classList.add("hidden");
  els.packagePurchaseMessage.innerHTML = "";
  els.signupForm.reset();
  saveState();
  render();
  renderSignupConfirmation(member);
  showCustomerStep("packages");
});

els.staffDoorCheck.addEventListener("click", () => runDoorCheck(els.staffCredential.value, els.staffVisitSelect.value, els.staffDoorResult));
els.kioskSubmit.addEventListener("click", runKioskCheck);
els.accountEmail.addEventListener("input", clearMemberLogin);
els.accountPassword.addEventListener("input", clearMemberLogin);
els.memberLoginButton.addEventListener("click", loginMember);
els.memberLogoutButton.addEventListener("click", logoutMember);
els.checkoutBackButton.addEventListener("click", () => showCustomerStep(pendingCheckout?.source === "account" ? "account" : "packages"));
els.checkoutForm.addEventListener("submit", completeCheckout);
els.addSessionSlot.addEventListener("click", addPendingSessionSlot);
els.classPackageForm.addEventListener("submit", addStaffClassPackage);
els.marketingBuildCampaign.addEventListener("click", () => updateMarketingPreview(true));
els.marketingCopyEmails.addEventListener("click", copyMarketingEmails);
els.marketingCopyMessage.addEventListener("click", copyMarketingMessage);
[els.marketingStatusFilter, els.marketingConsentFilter, els.marketingPackageFilter, els.marketingSearch, els.marketingOffer].forEach((input) => {
  input.addEventListener(input.tagName === "INPUT" ? "input" : "change", () => updateMarketingPreview(false));
});
document.querySelectorAll("[data-member-tab]").forEach((button) => {
  button.addEventListener("click", () => showMemberTab(button.dataset.memberTab));
});

document.querySelectorAll("[data-staff-tab]").forEach((button) => {
  button.addEventListener("click", () => showStaffTab(button.dataset.staffTab));
});

document.querySelectorAll(".keypad-key").forEach((button) => {
  button.addEventListener("click", () => handleKioskKey(button.dataset.key));
});

els.connectStripe.addEventListener("click", () => {
  const tenant = selectedTenant();
  if (tenant.subscriptionStatus === "suspended") return;
  tenant.stripeConnected = true;
  saveState();
  render();
});

els.addTenant.addEventListener("click", () => {
  const count = state.tenants.length + 1;
  state.tenants.push({
    id: `tenant-${Date.now()}`,
    name: `Demo Gym ${count}`,
    location: "New location",
    addressLine1: "",
    addressLine2: "",
    postcode: "",
    contactName: "",
    contactEmail: "",
    contactPhone: "",
    subscriptionStatus: "active",
    salonMaxFee: 100,
    freeTrialUntil: addDaysIso(14),
    billingDay: 1,
    adminNotes: "",
    stripeConnected: false,
    classes: cloneDefaultClasses(),
    plans: [
      { id: `plan-${Date.now()}`, name: "Monthly membership", price: 35, durationDays: 30, billing: "monthly", entitlements: defaultClasses.map((item) => item.id) }
    ],
    members: [],
    accessLogs: []
  });
  saveState();
  render();
});

els.resetDemo.addEventListener("click", () => {
  localStorage.removeItem(STORAGE_KEY);
  state = loadState();
  render();
});

function render() {
  renderNavigation();
  renderTenantSelector();
  renderTenantStatus();
  renderCustomer();
  renderReception();
  renderStaff();
  renderOwner();
}

function renderNavigation() {
  if (SURFACE_VIEWS.has(SURFACE)) {
    state.view = SURFACE;
  }
  document.querySelectorAll(".nav-button").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.view === state.view);
  });
  document.querySelectorAll(".view").forEach((view) => {
    view.classList.toggle("is-active", view.id === `${state.view}View`);
  });
  const titles = {
    customer: "Customer portal",
    reception: "Reception check-in",
    staff: "Gym staff",
    owner: "Salon Max admin"
  };
  const surfaceTitles = {
    customer: "KADO Fitness customer website",
    reception: "KADO Fitness reception check-in",
    staff: "KADO Fitness staff management",
    owner: "Salon Max admin"
  };
  els.pageTitle.textContent = SURFACE === "all" ? titles[state.view] : surfaceTitles[state.view];
}

function renderTenantSelector() {
  els.tenantSelect.innerHTML = state.tenants
    .map((tenant) => `<option value="${tenant.id}">${escapeHtml(tenant.name)}</option>`)
    .join("");
  els.tenantSelect.value = state.selectedTenantId;
}

function renderTenantStatus() {
  const tenant = selectedTenant();
  const suspended = tenant.subscriptionStatus === "suspended";
  els.suspendedBanner.classList.toggle("hidden", !suspended);
  els.systemDot.classList.toggle("is-warn", suspended);
  els.systemStatus.textContent = suspended ? "Suspended" : "Operational";
  els.systemHint.textContent = suspended ? "Access blocked" : "Door API ready";
  els.payButton.disabled = suspended;
  els.connectStripe.disabled = suspended;
  els.kioskSubmit.disabled = suspended;
}

function renderCustomer() {
  const tenant = selectedTenant();
  normalizeGymCatalog(tenant);
  renderCustomerBranding(tenant);
  renderPublicPlanCatalog(tenant);
  renderPackageChoiceList(tenant);
  renderAccountSummary();
  renderMemberTabs();
  renderMemberBuyClassList();

  const latestMember = tenant.members.find((member) => member.id === state.latestMemberId);
  if (!latestMember) {
    els.latestMemberTitle.textContent = "No new member yet";
    els.latestMemberCard.className = "member-pass empty-state";
    els.latestMemberCard.textContent = "Create a member account, then buy a membership or class package.";
    return;
  }

  const plans = memberPlans(tenant, latestMember);
  els.latestMemberTitle.textContent = latestMember.name;
  els.latestMemberCard.className = "member-pass";
  els.latestMemberCard.innerHTML = `
    <div><strong>Account created</strong></div>
    <div>${escapeHtml(latestMember.email)}</div>
    <div>${plans.length ? `Purchased: ${escapeHtml(plans.map((plan) => plan.name).join(", "))}` : "No package purchased yet"}</div>
    <div>${plans.length ? "Member page active." : "Choose a package to activate access."}</div>
    <span class="badge ${plans.length ? "good" : ""}">${plans.length ? "Package active" : "Awaiting package"}</span>
  `;
}

function renderCustomerBranding(tenant) {
  const brand = tenant.brand || { name: tenant.name, tagline: "Join online in minutes." };
  const monthlyPlan = tenant.plans.find((plan) => plan.billing === "monthly");
  const oneOffPlan = tenant.plans.find((plan) => plan.billing === "one-off");
  els.publicBrandLabel.textContent = brand.name || tenant.name;
  els.publicHeroTitle.textContent = `Join ${brand.name || tenant.name}`;
  els.publicHeroTagline.textContent = brand.tagline || "Join online in minutes.";
  els.heroMembershipPrice.textContent = monthlyPlan ? `£${monthlyPlan.price}` : "Join";
  els.heroClassPrice.textContent = oneOffPlan ? `Pay per class £${oneOffPlan.price}` : "Class packages available";
  if (brand.heroImage) {
    els.customerHero.style.setProperty("--customer-hero-image", `url("${brand.heroImage}")`);
  }
}

function renderPublicPlanCatalog(tenant) {
  els.publicPlanCatalog.innerHTML = sellablePlans(tenant).map((plan) => `
    <article class="plan-option">
      <header>
        <strong>${escapeHtml(plan.name)}</strong>
        <span class="plan-price">£${plan.price}/${plan.billing === "monthly" ? "month" : "once"}</span>
      </header>
      <span>${escapeHtml(plan.description || planEntitlementText(tenant, plan))}</span>
      <span class="row-meta">Access length: ${escapeHtml(planDurationText(plan))}</span>
      <span class="row-meta">${escapeHtml(planEntitlementText(tenant, plan))}</span>
    </article>
  `).join("");
}

function renderPackageChoiceList(tenant) {
  const member = tenant.members.find((item) => item.id === state.latestMemberId);
  els.packageChoiceList.innerHTML = sellablePlans(tenant).map((plan) => `
    <article class="package-choice">
      <div>
        <strong>${escapeHtml(plan.name)}</strong>
        <span>${escapeHtml(plan.description || planEntitlementText(tenant, plan))}</span>
        <small>Access length: ${escapeHtml(planDurationText(plan))}</small>
        <small>${escapeHtml(planEntitlementText(tenant, plan))}</small>
      </div>
      <div>
        <strong>£${plan.price}</strong>
        <button class="primary-button" type="button" data-buy-plan="${plan.id}" ${member ? "" : "disabled"}>${member ? "Checkout" : "Create account first"}</button>
      </div>
    </article>
  `).join("");

  document.querySelectorAll("[data-buy-plan]").forEach((button) => {
    button.addEventListener("click", () => beginCheckout(state.latestMemberId, button.dataset.buyPlan, "join"));
  });
}

function renderSignupConfirmation(member) {
  els.signupConfirmation.classList.remove("hidden");
  els.signupConfirmation.innerHTML = `
    <strong>Member account created for ${escapeHtml(member.name)}</strong>
    <span>Login with ${escapeHtml(member.email)} to view PIN ${escapeHtml(member.credential)}.</span>
    <span>Now choose a membership or class package to activate access.</span>
  `;
}

function renderStaff() {
  const tenant = selectedTenant();
  normalizeGymCatalog(tenant);
  normalizeGymPayments(tenant);
  const activeMembers = tenant.members.filter((member) => member.status === "active" && !isExpired(member.expiresAt));
  const monthlyRevenue = paidPayments(tenant).reduce((total, payment) => total + Number(payment.amount || 0), 0);
  const attendanceLogs = grantedClassLogs(tenant);

  els.activeMembers.textContent = activeMembers.length;
  els.monthlyRevenue.textContent = `£${monthlyRevenue}`;
  els.accessAttempts.textContent = tenant.accessLogs.length;
  els.attendanceCount.textContent = attendanceLogs.length;
  els.stripeStatus.textContent = tenant.stripeConnected ? "Connected" : "Not connected";
  renderVisitOptions(tenant, els.staffVisitSelect);

  els.activeMemberList.innerHTML = tenant.members.length
    ? renderMemberTable(tenant, tenant.members)
    : `<div class="empty-state">No members yet.</div>`;
  els.expiredMemberList.innerHTML = "";

  els.accessLog.innerHTML = tenant.accessLogs.length
    ? tenant.accessLogs.slice(0, 12).map(renderLogRow).join("")
    : `<div class="empty-state">No door scans have been run yet.</div>`;

  renderAttendanceAnalytics(tenant);
  renderFinanceSuite(tenant);
  renderMarketingTools(tenant);
  renderPricingList(tenant);
  bindStaffMemberActions(tenant);
}

function renderReception() {
  const tenant = selectedTenant();
  normalizeGymCatalog(tenant);
  els.kioskGymName.textContent = tenant.name;
  renderVisitOptions(tenant, els.kioskVisitSelect);
  renderKioskPin();
}

function renderOwner() {
  const billableTenants = state.tenants.filter((tenant) => tenant.subscriptionStatus !== "suspended" && !isTenantInTrial(tenant));
  const suspendedTenants = state.tenants.filter((tenant) => tenant.subscriptionStatus === "suspended");
  els.tenantCount.textContent = state.tenants.length;
  els.ownerMrr.textContent = `£${billableTenants.reduce((total, tenant) => total + Number(tenant.salonMaxFee || 0), 0)}`;
  els.suspendedCount.textContent = suspendedTenants.length;
  els.totalMembers.textContent = state.tenants.reduce((total, tenant) => total + tenant.members.length, 0);

  els.tenantList.innerHTML = state.tenants.map(renderTenantRow).join("");
  document.querySelectorAll("[data-action='toggle-suspend']").forEach((button) => {
    button.addEventListener("click", () => {
      const tenant = state.tenants.find((item) => item.id === button.dataset.tenantId);
      tenant.subscriptionStatus = tenant.subscriptionStatus === "suspended" ? "active" : "suspended";
      saveState();
      render();
    });
  });

  document.querySelectorAll("[data-save-tenant-profile]").forEach((button) => {
    button.addEventListener("click", () => {
      const tenant = state.tenants.find((item) => item.id === button.dataset.saveTenantProfile);
      if (!tenant) return;

      tenant.name = valueForTenantField(tenant.id, "name") || tenant.name;
      tenant.location = valueForTenantField(tenant.id, "location");
      tenant.addressLine1 = valueForTenantField(tenant.id, "addressLine1");
      tenant.addressLine2 = valueForTenantField(tenant.id, "addressLine2");
      tenant.postcode = valueForTenantField(tenant.id, "postcode");
      tenant.contactName = valueForTenantField(tenant.id, "contactName");
      tenant.contactEmail = valueForTenantField(tenant.id, "contactEmail");
      tenant.contactPhone = valueForTenantField(tenant.id, "contactPhone");
      tenant.subscriptionStatus = valueForTenantField(tenant.id, "subscriptionStatus") || "active";
      tenant.salonMaxFee = Math.max(0, Number(valueForTenantField(tenant.id, "salonMaxFee") || 0));
      tenant.freeTrialUntil = dateFieldToIso(valueForTenantField(tenant.id, "freeTrialUntil"));
      tenant.billingDay = Math.min(28, Math.max(1, Number(valueForTenantField(tenant.id, "billingDay") || 1)));
      tenant.adminNotes = valueForTenantField(tenant.id, "adminNotes");
      tenant.stripeConnected = valueForTenantField(tenant.id, "stripeConnected") === "true";

      if (state.selectedTenantId === tenant.id) {
        state.latestMemberId = null;
      }

      saveState();
      render();
    });
  });

  document.querySelectorAll("[data-trial-action]").forEach((button) => {
    button.addEventListener("click", () => {
      const tenant = state.tenants.find((item) => item.id === button.dataset.tenantId);
      if (!tenant) return;
      tenant.freeTrialUntil = button.dataset.trialAction === "start" ? addDaysIso(14) : "";
      saveState();
      render();
    });
  });
}

function renderAccountSummary() {
  const tenant = selectedTenant();
  const member = loggedInMember();

  if (!loggedInMemberId) {
    els.accountSummary.className = "account-summary empty-state";
    els.accountSummary.textContent = "Log in to view your PIN, purchases, attendance, and buy another class.";
    return;
  }

  if (!member) return;

  els.accountSummary.className = "account-summary";
  els.accountSummary.innerHTML = `
    <div class="member-detail-grid">
      <div>
        <small>Name</small>
        <strong>${escapeHtml(member.name)}</strong>
      </div>
      <div>
        <small>Email</small>
        <strong>${escapeHtml(member.email)}</strong>
      </div>
      <div>
        <small>Member PIN</small>
        <strong>${escapeHtml(member.credential)}</strong>
      </div>
      <div>
        <small>Status</small>
        <strong>${isExpired(member.expiresAt) ? "Expired" : "Active"}</strong>
      </div>
    </div>
    <div>
      <h3>Entry PIN</h3>
      <div class="pass-code">${escapeHtml(member.credential)}</div>
    </div>
    <h3>Purchased classes and memberships</h3>
    <div class="package-list">
      ${memberPlans(tenant, member).map((plan) => `<span class="badge good">${escapeHtml(plan.name)}</span>`).join("")}
    </div>
    <span>Current access: ${escapeHtml(memberEntitlementText(tenant, member))}</span>
    <span>Paid access valid until ${formatDate(member.expiresAt)}</span>
    <h3>Update personal details</h3>
    <form class="member-details-form" id="memberDetailsForm">
      <label>
        Full name
        <input id="detailName" value="${escapeHtml(member.name)}">
      </label>
      <label>
        Email
        <input id="detailEmail" type="email" value="${escapeHtml(member.email)}">
      </label>
      <label>
        Address line 1
        <input id="detailAddress1" value="${escapeHtml(member.address?.line1 || "")}" placeholder="House number and street">
      </label>
      <label>
        Address line 2
        <input id="detailAddress2" value="${escapeHtml(member.address?.line2 || "")}" placeholder="Optional">
      </label>
      <label>
        Town / city
        <input id="detailCity" value="${escapeHtml(member.address?.city || "")}">
      </label>
      <label>
        Postcode
        <input id="detailPostcode" value="${escapeHtml(member.address?.postcode || "")}">
      </label>
      <label>
        New password
        <input id="detailPassword" type="password" placeholder="Leave blank to keep current password">
      </label>
      <button class="primary-button" type="submit">Save details</button>
    </form>
    <div id="memberDetailsMessage" class="empty-state"></div>
    <h3>Classes attended</h3>
    <div class="attendance-history">
      ${renderMemberAttendanceHistory(tenant, member)}
    </div>
  `;
  document.querySelector("#memberDetailsForm").addEventListener("submit", saveMemberDetails);
}

function loginMember() {
  const tenant = selectedTenant();
  const member = memberByLogin(tenant, els.accountEmail.value.trim(), els.accountPassword.value);
  if (!member) {
    loggedInMemberId = null;
    renderMemberTabs();
    renderAccountMessage("deny", "No member found for that email and password.");
    return;
  }
  loggedInMemberId = member.id;
  render();
}

function saveMemberDetails(event) {
  event.preventDefault();
  const member = loggedInMember();
  if (!member) return;

  member.name = document.querySelector("#detailName").value.trim() || member.name;
  member.email = document.querySelector("#detailEmail").value.trim() || member.email;
  member.address = {
    line1: document.querySelector("#detailAddress1").value.trim(),
    line2: document.querySelector("#detailAddress2").value.trim(),
    city: document.querySelector("#detailCity").value.trim(),
    postcode: document.querySelector("#detailPostcode").value.trim()
  };

  const newPassword = document.querySelector("#detailPassword").value;
  if (newPassword) {
    member.password = newPassword;
    els.accountPassword.value = newPassword;
  }
  els.accountEmail.value = member.email;

  saveState();
  render();
  const message = document.querySelector("#memberDetailsMessage");
  if (message) {
    message.textContent = "Details saved.";
  }
}

function clearMemberLogin() {
  loggedInMemberId = null;
  showMemberTab("overview");
  renderMemberTabs();
  renderAccountSummary();
}

function startMemberLogin() {
  loggedInMemberId = null;
  els.accountEmail.value = "";
  els.accountPassword.value = "";
  showMemberTab("overview");
  renderMemberTabs();
  renderAccountSummary();
}

function logoutMember() {
  loggedInMemberId = null;
  els.accountEmail.value = "";
  els.accountPassword.value = "";
  showMemberTab("overview");
  render();
}

function loggedInMember() {
  return selectedTenant().members.find((member) => member.id === loggedInMemberId);
}

function renderMemberTabs() {
  els.memberLoginForm.classList.toggle("hidden", Boolean(loggedInMemberId));
  els.memberSessionActions.classList.toggle("hidden", !loggedInMemberId);
  els.memberTabs.classList.toggle("hidden", !loggedInMemberId);
  if (!loggedInMemberId) {
    els.memberBuyPanel.classList.add("hidden");
  }
}

function showMemberTab(tab) {
  document.querySelectorAll("[data-member-tab]").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.memberTab === tab);
  });
  els.accountSummary.classList.toggle("hidden", tab !== "overview");
  els.memberBuyPanel.classList.toggle("hidden", tab !== "buy" || !loggedInMemberId);
}

function showStaffTab(tab) {
  document.querySelectorAll("[data-staff-tab]").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.staffTab === tab);
  });
  document.querySelectorAll("[data-staff-panel]").forEach((panel) => {
    panel.classList.toggle("is-active", panel.dataset.staffPanel === tab);
  });
}

function renderMemberBuyClassList() {
  const member = loggedInMember();
  els.memberBuyClassList.innerHTML = sellablePlans(selectedTenant()).map((plan) => `
    <article class="package-choice">
      <div>
        <strong>${escapeHtml(plan.name)}</strong>
        <span>${escapeHtml(plan.description || planEntitlementText(selectedTenant(), plan))}</span>
        <small>Access length: ${escapeHtml(planDurationText(plan))}</small>
        <small>${escapeHtml(planEntitlementText(selectedTenant(), plan))}</small>
      </div>
      <div>
        <strong>£${plan.price}</strong>
        <button class="primary-button" type="button" data-member-buy-plan="${plan.id}" ${member ? "" : "disabled"}>Checkout</button>
      </div>
    </article>
  `).join("");

  document.querySelectorAll("[data-member-buy-plan]").forEach((button) => {
    button.addEventListener("click", () => beginCheckout(loggedInMemberId, button.dataset.memberBuyPlan, "account"));
  });
}

function beginCheckout(memberId, planId, source) {
  const tenant = selectedTenant();
  const member = tenant.members.find((item) => item.id === memberId);
  const plan = tenant.plans.find((item) => item.id === planId);
  if (!member || !plan) return;

  pendingCheckout = { memberId, planId, source };
  savePendingCheckout(pendingCheckout);
  els.checkoutName.value = member.name;
  els.checkoutSummary.innerHTML = `
    <article class="checkout-card">
      <strong>${escapeHtml(plan.name)}</strong>
      <span>${escapeHtml(plan.description || planEntitlementText(tenant, plan))}</span>
      <span>${escapeHtml(planEntitlementText(tenant, plan))}</span>
      <span>PIN access valid for ${escapeHtml(planDurationText(plan))} after payment.</span>
      <strong>£${plan.price}${plan.billing === "monthly" ? " per month" : ""}</strong>
    </article>
  `;
  showCustomerStep("checkout");
}

async function completeCheckout(event) {
  event.preventDefault();
  if (!pendingCheckout) return;
  const tenant = selectedTenant();
  const member = tenant.members.find((item) => item.id === pendingCheckout.memberId);
  const plan = tenant.plans.find((item) => item.id === pendingCheckout.planId);
  if (!member || !plan) return;

  try {
    const response = await fetch(`/gym/${encodeURIComponent(BUSINESS_ID)}/checkout/session`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        member_id: member.id,
        member_name: member.name,
        member_email: member.email,
        plan_id: plan.id,
        plan_name: plan.name,
        amount: plan.price,
        billing: plan.billing
      })
    });
    const result = await response.json();
    if (result.payment_mode === "stripe_checkout" && result.checkout_url) {
      savePendingCheckout(pendingCheckout);
      window.location.href = result.checkout_url;
      return;
    }
    if (result.payment_mode === "setup_required" && result.demo_allowed) {
      addPlanToMember(member, pendingCheckout.planId, pendingCheckout.source);
      renderPackagePurchaseMessage(`${plan.name} added in demo mode. Real Stripe checkout is not connected yet.`);
    } else if (!response.ok) {
      renderPackagePurchaseMessage(result.message || "Checkout could not be started.");
    }
  } catch {
    renderPackagePurchaseMessage("Checkout could not be reached. No access has been activated yet.");
  } finally {
    pendingCheckout = null;
    els.checkoutForm.reset();
  }
}

function savePendingCheckout(checkout) {
  localStorage.setItem(CHECKOUT_RETURN_KEY, JSON.stringify({
    ...checkout,
    businessId: BUSINESS_ID,
    createdAt: new Date().toISOString()
  }));
}

function loadPendingCheckout() {
  try {
    return JSON.parse(localStorage.getItem(CHECKOUT_RETURN_KEY) || "null");
  } catch {
    return null;
  }
}

function clearPendingCheckout() {
  localStorage.removeItem(CHECKOUT_RETURN_KEY);
}

async function processCheckoutReturn() {
  const params = new URLSearchParams(window.location.search);
  const checkoutStatus = params.get("checkout");
  const sessionId = params.get("session_id");
  if (!checkoutStatus) return;

  showCustomerStep("account");
  if (checkoutStatus === "cancelled") {
    renderPackagePurchaseMessage("Checkout was cancelled. No access has been activated.");
    clearPendingCheckout();
    cleanCheckoutUrl();
    return;
  }

  const savedCheckout = loadPendingCheckout();
  if (!sessionId || !savedCheckout || savedCheckout.businessId !== BUSINESS_ID) {
    renderPackagePurchaseMessage("Payment return could not be matched to this browser session. Staff can verify it in Stripe.");
    cleanCheckoutUrl();
    return;
  }

  try {
    const response = await fetch(`/gym/${encodeURIComponent(BUSINESS_ID)}/checkout/session/${encodeURIComponent(sessionId)}`);
    const result = await response.json();
    if (response.ok && result.confirmed) {
      const tenant = selectedTenant();
      const member = tenant.members.find((item) => item.id === savedCheckout.memberId);
      const plan = tenant.plans.find((item) => item.id === savedCheckout.planId);
      if (member && plan) {
        addPlanToMember(member, plan.id, "stripe checkout confirmed");
        renderPackagePurchaseMessage(`${plan.name} payment confirmed by Stripe. Access is now active.`);
        clearPendingCheckout();
      }
    } else {
      renderPackagePurchaseMessage(result.message || "Stripe has not confirmed this payment yet. No access has been activated.");
    }
  } catch {
    renderPackagePurchaseMessage("Could not verify the Stripe payment yet. No access has been activated.");
  } finally {
    cleanCheckoutUrl();
  }
}

function cleanCheckoutUrl() {
  const cleanUrl = `${window.location.origin}${window.location.pathname}`;
  window.history.replaceState({}, "", cleanUrl);
}

function addPlanToMember(member, planId, source) {
  const tenant = selectedTenant();
  const plan = tenant.plans.find((item) => item.id === planId);

  if (!member || !plan) return;
  if (tenant.subscriptionStatus === "suspended") {
    renderSignupMessage("Package purchase blocked. This gym is suspended by Salon Max.");
    return;
  }

  member.planId = member.planId || plan.id;
  member.packageIds = Array.from(new Set([...memberPlanIds(member), plan.id]));
  member.expiresAt = addDaysIso(Math.max(plan.durationDays, daysUntil(member.expiresAt)));
  recordPayment(tenant, member, plan, source);
  loggedInMemberId = member.id;
  els.accountEmail.value = member.email;
  els.accountPassword.value = member.password;
  saveState();
  render();
  if (source === "account") {
    showMemberTab("overview");
    renderAccountMessage("allow", `${plan.name} added. Payment would go directly to ${tenant.name}.`);
    return;
  }
  renderPackagePurchaseMessage(`${plan.name} added. Payment would go directly to ${tenant.name}. Returning to your member page.`);
  showCustomerStep("account");
  showMemberTab("overview");
}

function renderAccountMessage(type, message) {
  els.accountSummary.className = `account-summary decision ${type}`;
  els.accountSummary.textContent = message;
}

function renderPackagePurchaseMessage(message) {
  els.packagePurchaseMessage.classList.remove("hidden");
  els.packagePurchaseMessage.innerHTML = `
    <strong>Package purchased</strong>
    <span>${escapeHtml(message)}</span>
    <button class="secondary-button" type="button" data-step-target="account">Go to member login</button>
  `;
  els.packagePurchaseMessage.querySelector("[data-step-target]").addEventListener("click", () => showCustomerStep("account"));
}

function renderMemberAttendanceHistory(tenant, member) {
  const logs = grantedClassLogs(tenant)
    .filter((log) => log.memberId === member.id)
    .slice(0, 10);

  if (!logs.length) {
    return `<div class="empty-state">No attended classes logged yet.</div>`;
  }

  return logs.map((log) => `
    <article class="mini-log-row">
      <strong>${escapeHtml(log.visitName || "Class")}</strong>
      <span>${formatDateTime(log.createdAt)}</span>
    </article>
  `).join("");
}

function renderAttendanceAnalytics(tenant) {
  const logs = grantedClassLogs(tenant);

  if (!logs.length) {
    els.attendanceAnalytics.innerHTML = `<div class="empty-state">No successful class attendance yet.</div>`;
    return;
  }

  els.attendanceAnalytics.innerHTML = `
    <div>
      <h3>Most popular classes</h3>
      <div class="analytics-list">${renderCountList(countBy(logs, "visitName"))}</div>
    </div>
    <div>
      <h3>Busy times</h3>
      <div class="analytics-list">${renderCountList(countByHour(logs))}</div>
    </div>
    <div>
      <h3>Recent attendance</h3>
      <div class="analytics-list">${logs.slice(0, 6).map((log) => `
        <article class="mini-log-row">
          <strong>${escapeHtml(log.memberName || "Member")}</strong>
          <span>${escapeHtml(log.visitName || "Class")} - ${formatDateTime(log.createdAt)}</span>
        </article>
      `).join("")}</div>
    </div>
  `;
}

function renderFinanceSuite(tenant) {
  normalizeGymPayments(tenant);
  const payments = tenant.payments;
  const paid = paidPayments(tenant);
  const pending = payments.filter((payment) => payment.status === "pending");
  const failed = payments.filter((payment) => payment.status === "failed");
  const unpaidMembers = membersNeedingPayment(tenant);
  const totalPaid = paid.reduce((total, payment) => total + Number(payment.amount || 0), 0);
  const monthlyRecurring = paid
    .filter((payment) => payment.billing === "monthly")
    .reduce((total, payment) => total + Number(payment.amount || 0), 0);

  els.financeSummary.innerHTML = `
    <div class="stat"><small>Total paid</small><strong>£${totalPaid.toFixed(2)}</strong></div>
    <div class="stat"><small>Monthly recurring</small><strong>£${monthlyRecurring.toFixed(2)}</strong></div>
    <div class="stat"><small>Unpaid / expired</small><strong>${unpaidMembers.length}</strong></div>
    <div class="stat"><small>Pending / failed</small><strong>${pending.length + failed.length}</strong></div>
  `;

  els.classRevenueList.innerHTML = renderClassRevenueList(tenant);
  els.unpaidMemberList.innerHTML = unpaidMembers.length
    ? unpaidMembers.map((member) => renderUnpaidMemberRow(tenant, member)).join("")
    : `<div class="empty-state">No unpaid members need attention.</div>`;
  els.paymentLedger.innerHTML = payments.length
    ? renderPaymentLedger(tenant, payments)
    : `<div class="empty-state">No payments recorded yet.</div>`;

  bindFinanceActions(tenant);
}

function renderClassRevenueList(tenant) {
  const rows = classRevenueSummary(tenant);
  if (!rows.length) return `<div class="empty-state">No class revenue yet.</div>`;

  return rows.map((row) => `
    <article class="finance-row">
      <div>
        <strong>${escapeHtml(row.name)}</strong>
        <small>${row.attendanceCount} attendances</small>
      </div>
      <div>
        <span class="badge good">£${row.allocatedRevenue.toFixed(2)}</span>
        <small>Allocated revenue</small>
      </div>
      <div>
        <strong>£${row.directRevenue.toFixed(2)}</strong>
        <small>Direct package sales</small>
      </div>
      <div>
        <strong>${row.purchaseCount}</strong>
        <small>Purchases</small>
      </div>
    </article>
  `).join("");
}

function renderUnpaidMemberRow(tenant, member) {
  const status = memberPaymentStatus(tenant, member);
  return `
    <article class="finance-row">
      <div>
        <strong>${escapeHtml(member.name)}</strong>
        <small>${escapeHtml(member.email || "No email")} ${member.phone ? `- ${escapeHtml(member.phone)}` : ""}</small>
      </div>
      <div>
        <span class="badge bad">${escapeHtml(status.label)}</span>
        <small>${escapeHtml(status.detail)}</small>
      </div>
      <div>
        <strong>${memberPlans(tenant, member).map((plan) => escapeHtml(plan.name)).join(", ") || "No package"}</strong>
        <small>Access until ${formatDate(member.expiresAt)}</small>
      </div>
      <div class="finance-actions">
        <button class="secondary-button" type="button" data-finance-mark-paid="${member.id}">Record payment</button>
        <button class="secondary-button" type="button" data-finance-revoke="${member.id}">Revoke access</button>
      </div>
    </article>
  `;
}

function renderPaymentLedger(tenant, payments) {
  return `
    <div class="member-table-wrap">
      <table class="member-table payment-table">
        <thead>
          <tr>
            <th>Date</th>
            <th>Member</th>
            <th>Package</th>
            <th>Amount</th>
            <th>Status</th>
            <th>Source</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          ${payments
            .slice()
            .sort((a, b) => new Date(b.createdAt) - new Date(a.createdAt))
            .map((payment) => `
              <tr>
                <td>${formatDateTime(payment.createdAt)}</td>
                <td>${escapeHtml(payment.memberName || memberNameById(tenant, payment.memberId))}</td>
                <td>${escapeHtml(payment.planName || planNameById(tenant, payment.planId))}</td>
                <td><strong>£${Number(payment.amount || 0).toFixed(2)}</strong></td>
                <td><span class="badge ${payment.status === "paid" ? "good" : "bad"}">${escapeHtml(payment.status)}</span></td>
                <td>${escapeHtml(payment.source || "customer checkout")}</td>
                <td class="finance-actions">
                  <button class="secondary-button" type="button" data-payment-status="${payment.id}:paid">Paid</button>
                  <button class="secondary-button" type="button" data-payment-status="${payment.id}:pending">Pending</button>
                  <button class="danger-button" type="button" data-payment-status="${payment.id}:refunded">Refunded</button>
                </td>
              </tr>
            `).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function bindFinanceActions(tenant) {
  document.querySelectorAll("[data-finance-mark-paid]").forEach((button) => {
    button.addEventListener("click", () => {
      const member = tenant.members.find((item) => item.id === button.dataset.financeMarkPaid);
      const plan = memberPlans(tenant, member)[0] || sellablePlans(tenant)[0] || tenant.plans[0];
      if (!member || !plan) return;
      member.status = "active";
      if (isExpired(member.expiresAt)) {
        member.expiresAt = addDaysIso(Number(plan.durationDays || 30));
      }
      recordPayment(tenant, member, plan, "staff manual payment");
      saveState();
      render();
      showStaffTab("finance");
    });
  });

  document.querySelectorAll("[data-finance-revoke]").forEach((button) => {
    button.addEventListener("click", () => {
      const member = tenant.members.find((item) => item.id === button.dataset.financeRevoke);
      if (!member) return;
      member.status = "revoked";
      member.expiresAt = new Date().toISOString();
      saveState();
      render();
      showStaffTab("finance");
    });
  });

  document.querySelectorAll("[data-payment-status]").forEach((button) => {
    button.addEventListener("click", () => {
      const [paymentId, status] = button.dataset.paymentStatus.split(":");
      const payment = tenant.payments.find((item) => item.id === paymentId);
      if (!payment) return;
      payment.status = status;
      payment.updatedAt = new Date().toISOString();
      saveState();
      render();
      showStaffTab("finance");
    });
  });
}

function renderCountList(counts) {
  return Object.entries(counts)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 6)
    .map(([label, count]) => `
      <article class="mini-log-row">
        <strong>${escapeHtml(label)}</strong>
        <span>${count} attended</span>
      </article>
    `).join("");
}

function countBy(logs, key) {
  return logs.reduce((counts, log) => {
    const label = log[key] || "Unknown";
    counts[label] = (counts[label] || 0) + 1;
    return counts;
  }, {});
}

function countByHour(logs) {
  return logs.reduce((counts, log) => {
    const date = new Date(log.createdAt);
    const label = `${String(date.getHours()).padStart(2, "0")}:00`;
    counts[label] = (counts[label] || 0) + 1;
    return counts;
  }, {});
}

function renderMarketingTools(tenant) {
  const currentPackage = els.marketingPackageFilter.value || "all";
  els.marketingPackageFilter.innerHTML = `
    <option value="all">Any package or class</option>
    ${tenant.plans.map((plan) => `<option value="${plan.id}">${escapeHtml(plan.name)}</option>`).join("")}
  `;
  els.marketingPackageFilter.value = tenant.plans.some((plan) => plan.id === currentPackage) ? currentPackage : "all";
  updateMarketingPreview(false);
}

function updateMarketingPreview(forceMessageRefresh = false) {
  const tenant = selectedTenant();
  const recipients = filteredMarketingMembers(tenant);
  const offer = els.marketingOffer.value.trim() || "Come back this week with a member-only offer";
  const audienceLabel = marketingAudienceLabel();
  const subject = `${tenant.name}: ${audienceLabel}`;
  const message = [
    `Hi {first_name},`,
    "",
    `We would love to see you back at ${tenant.name}.`,
    offer,
    "",
    "Reply to this email or log in to your member portal to restart your access.",
    "",
    `${tenant.name}`
  ].join("\n");

  if (forceMessageRefresh || !els.marketingSubject.value.trim()) {
    els.marketingSubject.value = subject;
  }
  if (forceMessageRefresh || !els.marketingBody.value.trim()) {
    els.marketingBody.value = message;
  }

  els.marketingRecipientCount.textContent = `${recipients.length} matched`;
  els.marketingPreview.innerHTML = recipients.length
    ? renderMarketingRecipientTable(tenant, recipients)
    : `<div class="empty-state">No members match these filters.</div>`;
  els.marketingCopyStatus.className = "decision neutral";
  els.marketingCopyStatus.textContent = recipients.length
    ? "Preview ready. Copy emails or message when you are happy with it."
    : "No matching email recipients yet.";
}

function filteredMarketingMembers(tenant) {
  const statusFilter = els.marketingStatusFilter.value || "expired";
  const consentFilter = els.marketingConsentFilter.value || "email_consent";
  const packageFilter = els.marketingPackageFilter.value || "all";
  const search = els.marketingSearch.value.trim().toLowerCase();

  return tenant.members
    .filter((member) => {
      normalizeMemberProfile(member);
      const plans = memberPlans(tenant, member);
      const expired = isExpired(member.expiresAt);
      const status = member.status || "active";

      if (statusFilter === "expired" && !expired) return false;
      if (statusFilter === "expires_14" && (expired || daysUntil(member.expiresAt) > 14)) return false;
      if (statusFilter === "active" && (status !== "active" || expired)) return false;
      if (statusFilter === "paused" && status !== "paused") return false;
      if (statusFilter === "revoked" && status !== "revoked") return false;
      if (statusFilter === "no_package" && plans.length) return false;

      if (consentFilter === "email_consent" && !member.marketingConsent) return false;
      if (consentFilter === "no_consent" && member.marketingConsent) return false;
      if (!member.email) return false;

      if (packageFilter !== "all" && !plans.some((plan) => plan.id === packageFilter)) return false;

      if (search) {
        const haystack = [
          member.name,
          member.email,
          member.phone,
          member.marketingSource,
          member.notes,
          member.address?.line1,
          member.address?.postcode
        ].join(" ").toLowerCase();
        if (!haystack.includes(search)) return false;
      }

      return true;
    })
    .sort((a, b) => new Date(a.expiresAt) - new Date(b.expiresAt));
}

function renderMarketingRecipientTable(tenant, recipients) {
  return `
    <div class="member-table-wrap">
      <table class="member-table marketing-table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Email</th>
            <th>Status</th>
            <th>Package</th>
            <th>Access until</th>
            <th>Source / notes</th>
          </tr>
        </thead>
        <tbody>
          ${recipients.map((member) => {
            const plans = memberPlans(tenant, member);
            const expired = isExpired(member.expiresAt);
            const statusText = member.status !== "active" ? member.status : expired ? "expired" : "active";
            return `
              <tr class="${statusText !== "active" ? "is-muted" : ""}">
                <td><strong>${escapeHtml(member.name)}</strong><small>${escapeHtml(member.phone || "No phone")}</small></td>
                <td>${escapeHtml(member.email)}</td>
                <td><span class="badge ${statusText === "active" ? "good" : "bad"}">${escapeHtml(statusText)}</span></td>
                <td>${plans.length ? escapeHtml(plans.map((plan) => plan.name).join(", ")) : "No package"}</td>
                <td>${formatDate(member.expiresAt)}</td>
                <td>${escapeHtml([member.marketingSource, member.notes].filter(Boolean).join(" - ") || "No notes")}</td>
              </tr>
            `;
          }).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function marketingAudienceLabel() {
  const option = els.marketingStatusFilter.selectedOptions[0];
  return option ? option.textContent.toLowerCase() : "member offer";
}

function copyMarketingEmails() {
  const recipients = filteredMarketingMembers(selectedTenant());
  copyMarketingText(recipients.map((member) => member.email).join(", "), `${recipients.length} email ${recipients.length === 1 ? "address" : "addresses"} copied.`);
}

function copyMarketingMessage() {
  const text = `Subject: ${els.marketingSubject.value.trim()}\n\n${els.marketingBody.value.trim()}`;
  copyMarketingText(text, "Email subject and message copied.");
}

function copyMarketingText(text, successMessage) {
  if (!text.trim()) {
    els.marketingCopyStatus.className = "decision deny";
    els.marketingCopyStatus.textContent = "Nothing to copy yet.";
    return;
  }

  navigator.clipboard.writeText(text).then(() => {
    els.marketingCopyStatus.className = "decision allow";
    els.marketingCopyStatus.textContent = successMessage;
  }).catch(() => {
    els.marketingCopyStatus.className = "decision neutral";
    els.marketingCopyStatus.textContent = "Copy blocked by browser. Select the text and copy it manually.";
  });
}

function normalizeGymPayments(tenant) {
  tenant.payments ||= [];
  tenant.members.forEach((member) => {
    normalizeMemberProfile(member);
    memberPlans(tenant, member).forEach((plan) => {
      const exists = tenant.payments.some((payment) => payment.memberId === member.id && payment.planId === plan.id);
      if (!exists) {
        tenant.payments.push(createPaymentRecord(tenant, member, plan, "legacy/imported package", member.createdAt || new Date().toISOString()));
      }
    });
  });
}

function recordPayment(tenant, member, plan, source) {
  tenant.payments ||= [];
  tenant.payments.unshift(createPaymentRecord(tenant, member, plan, source));
}

function createPaymentRecord(tenant, member, plan, source, createdAt = new Date().toISOString()) {
  return {
    id: `pay-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`,
    memberId: member.id,
    memberName: member.name,
    planId: plan.id,
    planName: plan.name,
    amount: Number(plan.price || 0),
    billing: plan.billing || "one-off",
    status: "paid",
    source,
    createdAt,
    updatedAt: createdAt
  };
}

function paidPayments(tenant) {
  return (tenant.payments || []).filter((payment) => payment.status === "paid");
}

function classRevenueSummary(tenant) {
  const rows = tenant.classes
    .filter((classItem) => !classItem.openAccess)
    .map((classItem) => ({
      id: classItem.id,
      name: classItem.name,
      allocatedRevenue: 0,
      directRevenue: 0,
      purchaseCount: 0,
      attendanceCount: grantedClassLogs(tenant).filter((log) => log.visitId === classItem.id).length
    }));

  const byId = new Map(rows.map((row) => [row.id, row]));
  paidPayments(tenant).forEach((payment) => {
    const plan = tenant.plans.find((item) => item.id === payment.planId);
    if (!plan) return;
    const classIds = plan.entitlements.filter((classId) => byId.has(classId));
    if (!classIds.length) return;
    const allocated = Number(payment.amount || 0) / classIds.length;
    classIds.forEach((classId) => {
      const row = byId.get(classId);
      row.allocatedRevenue += allocated;
      row.purchaseCount += 1;
      if (classIds.length === 1) row.directRevenue += Number(payment.amount || 0);
    });
  });

  return rows.sort((a, b) => b.allocatedRevenue - a.allocatedRevenue || b.attendanceCount - a.attendanceCount);
}

function membersNeedingPayment(tenant) {
  return tenant.members.filter((member) => {
    const status = memberPaymentStatus(tenant, member);
    return status.needsAttention;
  });
}

function memberPaymentStatus(tenant, member) {
  const plans = memberPlans(tenant, member);
  const payments = (tenant.payments || []).filter((payment) => payment.memberId === member.id);
  const hasPaid = payments.some((payment) => payment.status === "paid");
  const hasPending = payments.some((payment) => payment.status === "pending");
  const expired = isExpired(member.expiresAt);

  if (!plans.length) {
    return { label: "unpaid", detail: "No package bought", needsAttention: true };
  }
  if (member.status === "revoked") {
    return { label: "revoked", detail: "Access revoked", needsAttention: true };
  }
  if (hasPending) {
    return { label: "pending", detail: "Payment pending", needsAttention: true };
  }
  if (!hasPaid) {
    return { label: "unpaid", detail: "Package assigned but no paid record", needsAttention: true };
  }
  if (expired) {
    return { label: "expired", detail: "Paid before, access expired", needsAttention: true };
  }
  return { label: "paid", detail: "Paid and access active", needsAttention: false };
}

function memberNameById(tenant, memberId) {
  return tenant.members.find((member) => member.id === memberId)?.name || "Unknown member";
}

function planNameById(tenant, planId) {
  return tenant.plans.find((plan) => plan.id === planId)?.name || "Unknown package";
}

function renderSignupMessage(message) {
  els.signupConfirmation.classList.remove("hidden");
  els.signupConfirmation.textContent = message;
}

function addStaffClassPackage(event) {
  event.preventDefault();
  const tenant = selectedTenant();
  const classNameValue = els.newClassName.value.trim();
  const price = Math.max(0, Number(els.newClassPrice.value || 0));
  const durationWeeks = Math.max(1, Number(els.newClassDurationWeeks.value || 4));
  const durationDays = durationWeeks * 7;
  const sessions = pendingClassSessions.map((session) => ({ ...session }));

  if (!classNameValue) {
    showSessionPreviewMessage("Add the class name first.");
    return;
  }

  if (!sessions.length) {
    showSessionPreviewMessage("Add at least one weekly session time.");
    return;
  }

  const classId = createSlug(classNameValue);
  const uniqueClassId = uniqueId(classId, tenant.classes.map((item) => item.id));
  const planId = `plan-${uniqueClassId}`;

  tenant.classes.push({
    id: uniqueClassId,
    name: classNameValue,
    description: `${classNameValue} access. PIN access lasts ${durationWeeks} ${durationWeeks === 1 ? "week" : "weeks"} from payment.`,
    sessions
  });
  tenant.plans.push({
    id: planId,
    name: `${classNameValue} ${durationWeeks} week package`,
    price,
    durationDays,
    billing: "one-off",
    description: `${classNameValue} classes only. PIN access lasts ${durationWeeks} ${durationWeeks === 1 ? "week" : "weeks"} from payment.`,
    entitlements: [uniqueClassId],
    publicVisible: true
  });

  els.classPackageForm.reset();
  clearPendingClassSessions();
  saveState();
  render();
}

function addPendingSessionSlot() {
  const selectedDays = Array.from(document.querySelectorAll('input[name="classDays"]:checked')).map((input) => Number(input.value));
  const time = els.newSessionTime.value || "18:30";
  const durationMinutes = Math.max(15, Number(els.newSessionDuration.value || 60));

  if (!selectedDays.length) {
    showSessionPreviewMessage("Select at least one day for this session time.");
    return;
  }

  selectedDays.forEach((day) => {
    const exists = pendingClassSessions.some((session) => Number(session.day) === day && session.time === time);
    if (!exists) {
      pendingClassSessions.push({ day, time, durationMinutes });
    }
  });

  pendingClassSessions.sort((a, b) => Number(a.day) - Number(b.day) || timeToMinutes(a.time) - timeToMinutes(b.time));
  renderSessionPreview();
}

function renderSessionPreview() {
  if (!pendingClassSessions.length) {
    els.sessionPreview.className = "session-preview empty-state";
    els.sessionPreview.textContent = "No sessions added yet.";
    return;
  }

  els.sessionPreview.className = "session-preview";
  els.sessionPreview.innerHTML = pendingClassSessions.map((session, index) => `
    <span class="session-chip">
      ${escapeHtml(dayName(session.day))} ${escapeHtml(session.time)} (${Number(session.durationMinutes || 60)} mins)
      <button type="button" data-remove-session="${index}" aria-label="Remove session">x</button>
    </span>
  `).join("");

  document.querySelectorAll("[data-remove-session]").forEach((button) => {
    button.addEventListener("click", () => {
      pendingClassSessions.splice(Number(button.dataset.removeSession), 1);
      renderSessionPreview();
    });
  });
}

function showSessionPreviewMessage(message) {
  els.sessionPreview.className = "session-preview decision deny";
  els.sessionPreview.textContent = message;
}

function clearPendingClassSessions() {
  pendingClassSessions = [];
  document.querySelectorAll('input[name="classDays"]').forEach((input) => {
    input.checked = false;
  });
  renderSessionPreview();
}

function renderPricingList(tenant) {
  normalizeGymCatalog(tenant);
  els.pricingList.innerHTML = `
    <div class="class-admin-list">
      ${tenant.classes.map((classItem) => renderClassAdminRow(tenant, classItem)).join("")}
    </div>
  `;

  document.querySelectorAll("[data-save-class]").forEach((button) => {
    button.addEventListener("click", () => {
      const classId = button.dataset.saveClass;
      const classItem = classById(tenant, classId);
      if (!classItem) return;
      normalizeClassItem(classItem);
      const nameInput = document.querySelector(`[data-class-name="${classId}"]`);
      const descriptionInput = document.querySelector(`[data-class-description="${classId}"]`);
      const priceInput = document.querySelector(`[data-class-price="${classId}"]`);
      const durationInput = document.querySelector(`[data-class-duration="${classId}"]`);
      const publicInput = document.querySelector(`[data-class-public="${classId}"]`);
      const newName = nameInput.value.trim() || classItem.name;
      classItem.name = newName;
      classItem.description = descriptionInput.value.trim();
      const plan = ensureClassPlan(tenant, classItem);
      if (plan) {
        const weeks = Math.max(1, Number(durationInput.value || 4));
        plan.name = classItem.openAccess ? "KADO Membership" : `${newName} ${weeks} week package`;
        plan.price = Math.max(0, Number(priceInput.value || 0));
        plan.durationDays = Math.max(1, weeks * 7);
        plan.description = classItem.description || `${newName} access. PIN access lasts ${weeks} ${weeks === 1 ? "week" : "weeks"} from payment.`;
        plan.publicVisible = Boolean(publicInput?.checked);
      }
      saveState();
      render();
    });
  });

  document.querySelectorAll("[data-toggle-public]").forEach((button) => {
    button.addEventListener("click", () => {
      const plan = tenant.plans.find((item) => item.id === button.dataset.togglePublic);
      plan.publicVisible = plan.publicVisible === false;
      saveState();
      render();
    });
  });
}

function renderClassAdminRow(tenant, classItem) {
  normalizeClassItem(classItem);
  const plan = classPrimaryPlan(tenant, classItem.id);
  const durationWeeks = Math.max(1, Math.round(Number(plan?.durationDays || 28) / 7));
  const description = classItem.description || plan?.description || "";
  return `
    <article class="class-admin-row">
      <div>
        <strong>${escapeHtml(classItem.name)}</strong>
        <small>${escapeHtml(classItem.openAccess ? "Open access / membership" : planScheduleText(tenant, { entitlements: [classItem.id] }))}</small>
      </div>
      <label>
        Class name
        <input value="${escapeHtml(classItem.name)}" data-class-name="${classItem.id}">
      </label>
      <label class="class-description-field">
        Description
        <textarea data-class-description="${classItem.id}" placeholder="Shown on customer portal">${escapeHtml(description)}</textarea>
      </label>
      <label>
        Price
        <input type="number" min="0" step="1" value="${Number(plan?.price || 0)}" data-class-price="${classItem.id}">
      </label>
      <label>
        Length
        <select data-class-duration="${classItem.id}">
          ${[1, 4, 6, 8, 12].map((weeks) => `<option value="${weeks}" ${durationWeeks === weeks ? "selected" : ""}>${weeks} ${weeks === 1 ? "week" : "weeks"}</option>`).join("")}
        </select>
      </label>
      <label class="check-label">
        <input type="checkbox" ${plan?.publicVisible === false ? "" : "checked"} data-class-public="${classItem.id}">
        Published
      </label>
      <button class="secondary-button" type="button" data-save-class="${classItem.id}">Save</button>
    </article>
  `;
}

function classPrimaryPlan(tenant, classId) {
  return tenant.plans.find((plan) => Array.isArray(plan.entitlements) && plan.entitlements.length === 1 && plan.entitlements[0] === classId)
    || tenant.plans.find((plan) => Array.isArray(plan.entitlements) && plan.entitlements.includes(classId));
}

function ensureClassPlan(tenant, classItem) {
  let plan = classPrimaryPlan(tenant, classItem.id);
  if (plan) return plan;
  plan = {
    id: `plan-${classItem.id}`,
    name: `${classItem.name} 4 week package`,
    price: 18,
    durationDays: 28,
    billing: "one-off",
    description: `${classItem.name} access. PIN access lasts 4 weeks from payment.`,
    entitlements: [classItem.id],
    publicVisible: true
  };
  tenant.plans.push(plan);
  return plan;
}

function normalizeGymCatalog(tenant) {
  tenant.classes.forEach(normalizeClassItem);
  tenant.plans.forEach((plan) => {
    if (plan.publicVisible === undefined) plan.publicVisible = true;
  });
}

function normalizeClassItem(classItem) {
  classItem.description ||= "";
  classItem.sessions ||= [];
}

function renderMemberRow(tenant, member) {
  normalizeMemberProfile(member);
  const plans = memberPlans(tenant, member);
  const expired = isExpired(member.expiresAt);
  const status = member.status || "active";
  const rowStatus = status !== "active" ? status : expired ? "expired" : "active";
  const paymentStatus = memberPaymentStatus(tenant, member);
  return `
    <tr class="member-table-row ${rowStatus !== "active" ? "is-muted" : ""}">
      <td>
        <input value="${escapeHtml(member.name)}" data-staff-member-field="${member.id}:name" aria-label="Member name">
        <small>${escapeHtml(member.credentialType)} ${escapeHtml(member.credential)}</small>
      </td>
      <td>
        <input type="email" value="${escapeHtml(member.email)}" data-staff-member-field="${member.id}:email" aria-label="Email">
        <input value="${escapeHtml(member.phone || "")}" data-staff-member-field="${member.id}:phone" aria-label="Phone">
      </td>
      <td>
        <span class="badge ${rowStatus === "active" ? "good" : "bad"}">${escapeHtml(rowStatus)}</span>
        <select data-staff-member-field="${member.id}:status" aria-label="Status">
          <option value="active" ${status === "active" ? "selected" : ""}>Active</option>
          <option value="paused" ${status === "paused" ? "selected" : ""}>Paused</option>
          <option value="revoked" ${status === "revoked" ? "selected" : ""}>Revoked</option>
        </select>
      </td>
      <td>
        <strong>${plans.length ? escapeHtml(plans.map((plan) => plan.name).join(", ")) : "No package"}</strong>
        <small>${plans.length ? escapeHtml(memberEntitlementText(tenant, member)) : "No access yet"}</small>
      </td>
      <td>
        <span class="badge ${paymentStatus.needsAttention ? "bad" : "good"}">${escapeHtml(paymentStatus.label)}</span>
        <small>${escapeHtml(paymentStatus.detail)}</small>
      </td>
      <td>
        <input type="date" value="${escapeHtml(dateInputValue(member.expiresAt))}" data-staff-member-field="${member.id}:expiresAt" aria-label="Expiry date">
      </td>
      <td>
        <input value="${escapeHtml(member.address?.line1 || "")}" data-staff-member-field="${member.id}:address1" placeholder="Address line 1">
        <input value="${escapeHtml(member.address?.postcode || "")}" data-staff-member-field="${member.id}:postcode" placeholder="Postcode">
      </td>
      <td>
        <input value="${escapeHtml(member.marketingSource || "")}" data-staff-member-field="${member.id}:marketingSource" placeholder="Source">
        <label class="mini-check"><input type="checkbox" ${member.marketingConsent ? "checked" : ""} data-staff-member-field="${member.id}:marketingConsent"> Email</label>
        <label class="mini-check"><input type="checkbox" ${member.whatsappConsent ? "checked" : ""} data-staff-member-field="${member.id}:whatsappConsent"> WhatsApp</label>
      </td>
      <td>
        <textarea data-staff-member-field="${member.id}:notes" placeholder="Customer notes">${escapeHtml(member.notes || "")}</textarea>
      </td>
      <td class="member-actions-cell">
        <button class="secondary-button" type="button" data-save-member="${member.id}">Save</button>
        <button class="secondary-button" type="button" data-free-trial-member="${member.id}">7 day trial</button>
        <button class="danger-button" type="button" data-revoke-member="${member.id}">Revoke</button>
        <button class="danger-button" type="button" data-delete-member="${member.id}">Delete</button>
      </td>
    </tr>
  `;
}

function renderMemberTable(tenant, members) {
  return `
    <div class="member-table-wrap">
      <table class="member-table">
        <thead>
          <tr>
            <th>Name / PIN</th>
            <th>Contact</th>
            <th>Status</th>
            <th>Packages</th>
            <th>Payment</th>
            <th>Access until</th>
            <th>Address</th>
            <th>Marketing</th>
            <th>Notes</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          ${members.map((member) => renderMemberRow(tenant, member)).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function bindStaffMemberActions(tenant) {
  document.querySelectorAll("[data-save-member]").forEach((button) => {
    button.addEventListener("click", () => {
      const member = tenant.members.find((item) => item.id === button.dataset.saveMember);
      if (!member) return;
      member.name = staffMemberFieldValue(member.id, "name") || member.name;
      member.email = staffMemberFieldValue(member.id, "email") || member.email;
      member.phone = staffMemberFieldValue(member.id, "phone");
      member.status = staffMemberFieldValue(member.id, "status") || "active";
      member.expiresAt = dateFieldToIso(staffMemberFieldValue(member.id, "expiresAt")) || member.expiresAt;
      member.address = {
        ...(member.address || {}),
        line1: staffMemberFieldValue(member.id, "address1"),
        postcode: staffMemberFieldValue(member.id, "postcode")
      };
      member.marketingSource = staffMemberFieldValue(member.id, "marketingSource");
      member.marketingConsent = staffMemberChecked(member.id, "marketingConsent");
      member.whatsappConsent = staffMemberChecked(member.id, "whatsappConsent");
      member.notes = staffMemberFieldValue(member.id, "notes");
      if (loggedInMemberId === member.id) {
        els.accountEmail.value = member.email;
      }
      saveState();
      render();
    });
  });

  document.querySelectorAll("[data-revoke-member]").forEach((button) => {
    button.addEventListener("click", () => {
      const member = tenant.members.find((item) => item.id === button.dataset.revokeMember);
      if (!member) return;
      member.status = "revoked";
      member.expiresAt = new Date().toISOString();
      saveState();
      render();
    });
  });

  document.querySelectorAll("[data-free-trial-member]").forEach((button) => {
    button.addEventListener("click", () => {
      const member = tenant.members.find((item) => item.id === button.dataset.freeTrialMember);
      if (!member) return;
      const firstPublicPlan = sellablePlans(tenant)[0] || tenant.plans[0];
      if (firstPublicPlan && !memberPlanIds(member).length) {
        member.planId = firstPublicPlan.id;
        member.packageIds = [firstPublicPlan.id];
      }
      member.status = "active";
      member.expiresAt = addDaysIso(Math.max(7, daysUntil(member.expiresAt)));
      saveState();
      render();
    });
  });

  document.querySelectorAll("[data-delete-member]").forEach((button) => {
    button.addEventListener("click", () => {
      const memberId = button.dataset.deleteMember;
      tenant.members = tenant.members.filter((member) => member.id !== memberId);
      if (loggedInMemberId === memberId) loggedInMemberId = null;
      if (state.latestMemberId === memberId) state.latestMemberId = null;
      saveState();
      render();
    });
  });
}

function staffMemberFieldValue(memberId, field) {
  const input = document.querySelector(`[data-staff-member-field="${memberId}:${field}"]`);
  return input ? input.value.trim() : "";
}

function staffMemberChecked(memberId, field) {
  const input = document.querySelector(`[data-staff-member-field="${memberId}:${field}"]`);
  return Boolean(input?.checked);
}

function renderLogRow(log) {
  return `
    <article class="row-card">
      <header>
        <strong>${escapeHtml(log.memberName || log.credential)}</strong>
        <span class="badge ${log.allowed ? "good" : "bad"}">${log.allowed ? "Granted" : "Denied"}</span>
      </header>
      <div class="row-meta">
        <span>${escapeHtml(log.credential)}</span>
        <span>${escapeHtml(log.visitName || "Entry")}</span>
        <span>${escapeHtml(log.reason)}</span>
        <span>${formatDateTime(log.createdAt)}</span>
      </div>
    </article>
  `;
}

function valueForTenantField(tenantId, field) {
  const input = document.querySelector(`[data-tenant-field="${tenantId}:${field}"]`);
  return input ? input.value.trim() : "";
}

function normalizeTenantProfile(tenant) {
  tenant.addressLine1 ||= "";
  tenant.addressLine2 ||= "";
  tenant.postcode ||= "";
  tenant.contactName ||= "";
  tenant.contactEmail ||= "";
  tenant.contactPhone ||= "";
  tenant.freeTrialUntil ||= "";
  tenant.billingDay ||= 1;
  tenant.adminNotes ||= "";
}

function normalizeMemberProfile(member) {
  member.phone ||= "";
  member.status ||= "active";
  member.address ||= {};
  member.address.line1 ||= "";
  member.address.postcode ||= "";
  member.notes ||= "";
  member.marketingConsent = Boolean(member.marketingConsent);
  member.whatsappConsent = Boolean(member.whatsappConsent);
  member.marketingSource ||= "";
}

function isTenantInTrial(tenant) {
  return Boolean(tenant.freeTrialUntil) && !isExpired(tenant.freeTrialUntil);
}

function dateInputValue(dateIso) {
  if (!dateIso) return "";
  return new Date(dateIso).toISOString().slice(0, 10);
}

function dateFieldToIso(dateValue) {
  if (!dateValue) return "";
  const [year, month, day] = dateValue.split("-").map(Number);
  if (!year || !month || !day) return "";
  return new Date(year, month - 1, day, 23, 59, 59, 999).toISOString();
}

function renderTenantRow(tenant) {
  normalizeTenantProfile(tenant);
  const activeMembers = tenant.members.filter((member) => member.status === "active" && !isExpired(member.expiresAt)).length;
  const trialActive = isTenantInTrial(tenant);
  const statusClass = tenant.subscriptionStatus === "suspended" || tenant.subscriptionStatus === "past_due" ? "bad" : "good";
  const statusText = tenant.subscriptionStatus === "suspended" ? "Suspended" : tenant.subscriptionStatus === "past_due" ? "Past due" : trialActive ? "Free trial" : "Active";
  const actionText = tenant.subscriptionStatus === "suspended" ? "Reactivate" : "Suspend";

  return `
    <article class="row-card tenant-card">
      <header>
        <div>
          <strong>${escapeHtml(tenant.name)}</strong>
          <div class="row-meta">
            <span>${escapeHtml(tenant.location || "No location set")}</span>
            <span>£${Number(tenant.salonMaxFee || 0)}/month software fee</span>
            <span>${trialActive ? `Trial until ${formatDate(tenant.freeTrialUntil)}` : "No active trial"}</span>
            <span>${activeMembers} active members</span>
            <span>${tenant.stripeConnected ? "Gym Stripe connected" : "Gym Stripe pending"}</span>
          </div>
        </div>
        <span class="badge ${statusClass}">${statusText}</span>
      </header>
      <div class="tenant-profile-grid">
        <label>
          Gym name
          <input value="${escapeHtml(tenant.name)}" data-tenant-field="${tenant.id}:name">
        </label>
        <label>
          Town / area
          <input value="${escapeHtml(tenant.location || "")}" data-tenant-field="${tenant.id}:location">
        </label>
        <label>
          Address line 1
          <input value="${escapeHtml(tenant.addressLine1 || "")}" data-tenant-field="${tenant.id}:addressLine1">
        </label>
        <label>
          Address line 2
          <input value="${escapeHtml(tenant.addressLine2 || "")}" data-tenant-field="${tenant.id}:addressLine2">
        </label>
        <label>
          Postcode
          <input value="${escapeHtml(tenant.postcode || "")}" data-tenant-field="${tenant.id}:postcode">
        </label>
        <label>
          Main contact
          <input value="${escapeHtml(tenant.contactName || "")}" data-tenant-field="${tenant.id}:contactName">
        </label>
        <label>
          Contact email
          <input type="email" value="${escapeHtml(tenant.contactEmail || "")}" data-tenant-field="${tenant.id}:contactEmail">
        </label>
        <label>
          Contact number
          <input value="${escapeHtml(tenant.contactPhone || "")}" data-tenant-field="${tenant.id}:contactPhone">
        </label>
        <label>
          Monthly software price
          <input type="number" min="0" step="1" value="${Number(tenant.salonMaxFee || 0)}" data-tenant-field="${tenant.id}:salonMaxFee">
        </label>
        <label>
          Free trial until
          <input type="date" value="${escapeHtml(dateInputValue(tenant.freeTrialUntil))}" data-tenant-field="${tenant.id}:freeTrialUntil">
        </label>
        <label>
          Billing day
          <input type="number" min="1" max="28" step="1" value="${Number(tenant.billingDay || 1)}" data-tenant-field="${tenant.id}:billingDay">
        </label>
        <label>
          Account status
          <select data-tenant-field="${tenant.id}:subscriptionStatus">
            <option value="active" ${tenant.subscriptionStatus === "active" ? "selected" : ""}>Active</option>
            <option value="past_due" ${tenant.subscriptionStatus === "past_due" ? "selected" : ""}>Past due</option>
            <option value="suspended" ${tenant.subscriptionStatus === "suspended" ? "selected" : ""}>Suspended</option>
          </select>
        </label>
        <label>
          Gym Stripe status
          <select data-tenant-field="${tenant.id}:stripeConnected">
            <option value="true" ${tenant.stripeConnected ? "selected" : ""}>Connected</option>
            <option value="false" ${!tenant.stripeConnected ? "selected" : ""}>Pending</option>
          </select>
        </label>
        <label class="tenant-notes">
          Salon Max notes
          <textarea data-tenant-field="${tenant.id}:adminNotes">${escapeHtml(tenant.adminNotes || "")}</textarea>
        </label>
      </div>
      <div class="tenant-actions">
        <button class="primary-button" data-save-tenant-profile="${tenant.id}">Save account</button>
        <button class="secondary-button" data-trial-action="start" data-tenant-id="${tenant.id}">Start 14 day trial</button>
        <button class="secondary-button" data-trial-action="end" data-tenant-id="${tenant.id}">End trial</button>
        <button class="${tenant.subscriptionStatus === "suspended" ? "secondary-button" : "danger-button"}" data-action="toggle-suspend" data-tenant-id="${tenant.id}">
          ${actionText}
        </button>
      </div>
    </article>
  `;
}

function runDoorCheck(rawCredential, visitId, outputElement) {
  const tenant = selectedTenant();
  const credential = rawCredential.trim();
  const decision = checkAccess(tenant, credential, visitId);
  tenant.accessLogs.unshift({
    memberId: decision.memberId,
    memberName: decision.memberName,
    credential: credential || "blank scan",
    visitId,
    visitName: className(tenant, visitId),
    allowed: decision.allowed,
    reason: decision.reason,
    createdAt: new Date().toISOString()
  });
  saveState();
  renderStaff();
  showDecision(outputElement, decision.allowed ? "allow" : "deny", decision.reason);
}

function handleKioskKey(key) {
  if (key === "clear") {
    kioskPin = "";
  } else if (key === "back") {
    kioskPin = kioskPin.slice(0, -1);
  } else if (kioskPin.length < 8) {
    kioskPin += key;
  }
  renderKioskPin();
}

function runKioskCheck() {
  const tenant = selectedTenant();
  const credential = kioskPin;
  const visitId = els.kioskVisitSelect.value;
  const decision = checkAccess(tenant, credential, visitId);
  const member = memberByCredential(tenant, credential);

  tenant.accessLogs.unshift({
    memberId: decision.memberId,
    memberName: decision.memberName,
    credential: credential || "blank scan",
    visitId,
    visitName: className(tenant, visitId),
    allowed: decision.allowed,
    reason: decision.reason,
    createdAt: new Date().toISOString()
  });

  saveState();
  renderStaff();
  showKioskResult(decision, member, visitId);
  kioskPin = "";
  renderKioskPin();
}

function renderKioskPin() {
  els.kioskPinDisplay.textContent = kioskPin ? "•".repeat(kioskPin.length) : "Enter PIN";
}

function showKioskResult(decision, member, visitId) {
  els.kioskResult.className = `kiosk-result ${decision.allowed ? "allow" : "deny"}`;
  els.kioskResult.innerHTML = decision.allowed
    ? `<strong>Access granted</strong><span>${escapeHtml(member.name)} can attend ${escapeHtml(className(selectedTenant(), visitId))}. Valid until ${formatDate(member.expiresAt)}.</span>`
    : `<strong>Access denied</strong><span>${escapeHtml(decision.reason)}</span>`;
}

function checkAccess(tenant, credential, visitId = "gym_floor") {
  const deny = (reason) => ({ allowed: false, reason });
  if (tenant.subscriptionStatus === "suspended") {
    return deny("Denied: gym account is suspended by Salon Max.");
  }
  if (!credential) {
    return deny("Denied: no credential presented.");
  }
  const member = memberByCredential(tenant, credential);
  if (!member) {
    return deny("Denied: credential not recognised at this gym.");
  }
  const denyForMember = (reason) => ({ allowed: false, reason, memberId: member.id, memberName: member.name });
  if (member.status !== "active") {
    return denyForMember(`Denied: member status is ${member.status}.`);
  }
  if (isExpired(member.expiresAt)) {
    return denyForMember("Denied: membership has expired.");
  }
  if (!memberEntitlements(tenant, member).includes(visitId)) {
    return denyForMember(`Denied: ${member.name} paid for ${memberEntitlementText(tenant, member)}, not ${className(tenant, visitId)}.`);
  }
  const timeDecision = checkClassTimeWindow(tenant, visitId);
  if (!timeDecision.allowed) {
    return denyForMember(timeDecision.reason);
  }
  const usageDecision = checkDailyClassUsage(tenant, member, visitId);
  if (!usageDecision.allowed) {
    return denyForMember(usageDecision.reason);
  }
  return {
    allowed: true,
    memberId: member.id,
    memberName: member.name,
    reason: `Granted: ${member.name} can attend ${className(tenant, visitId)} until ${formatDate(member.expiresAt)}.`
  };
}

function selectedTenant() {
  return state.tenants.find((tenant) => tenant.id === state.selectedTenantId) || state.tenants[0];
}

function planForMember(tenant, member) {
  return tenant.plans.find((plan) => plan.id === member.planId) || tenant.plans[0];
}

function memberByCredential(tenant, credential) {
  return tenant.members.find((item) => item.credential.toLowerCase() === credential.toLowerCase());
}

function memberByLogin(tenant, email, password) {
  return tenant.members.find((item) => item.email.toLowerCase() === email.toLowerCase() && item.password === password);
}

function memberPlanIds(member) {
  return member.packageIds?.length ? member.packageIds : [member.planId];
}

function memberPlans(tenant, member) {
  return memberPlanIds(member)
    .map((planId) => tenant.plans.find((plan) => plan.id === planId))
    .filter(Boolean);
}

function memberEntitlements(tenant, member) {
  return Array.from(new Set(memberPlans(tenant, member).flatMap((plan) => plan.entitlements)));
}

function memberEntitlementText(tenant, member) {
  const entitlements = memberEntitlements(tenant, member);
  if (entitlements.length === tenant.classes.length) return "All classes and gym floor";
  return entitlements.map((id) => className(tenant, id)).join(", ");
}

function grantedClassLogs(tenant) {
  return tenant.accessLogs.filter((log) => {
    const classItem = classById(tenant, log.visitId);
    return log.allowed && classItem && !classItem.openAccess;
  });
}

function sellablePlans(tenant) {
  return tenant.plans.filter((plan) => plan.publicVisible !== false);
}

function renderVisitOptions(tenant, selectElement) {
  const currentValue = selectElement.value || "gym_floor";
  selectElement.innerHTML = tenant.classes
    .map((item) => `<option value="${item.id}">${escapeHtml(item.name)}</option>`)
    .join("");
  selectElement.value = tenant.classes.some((item) => item.id === currentValue) ? currentValue : "gym_floor";
}

function planEntitlementText(tenant, plan) {
  if (plan.entitlements.length === tenant.classes.length) return "All classes and gym floor";
  return plan.entitlements.map((id) => className(tenant, id)).join(", ");
}

function planDurationText(plan) {
  const days = Number(plan.durationDays || 30);
  if (days % 7 === 0) {
    const weeks = days / 7;
    return `${weeks} ${weeks === 1 ? "week" : "weeks"}`;
  }
  return `${days} ${days === 1 ? "day" : "days"}`;
}

function className(tenant, classId) {
  return tenant.classes.find((item) => item.id === classId)?.name || "Entry";
}

function classById(tenant, classId) {
  return tenant.classes.find((item) => item.id === classId);
}

function checkClassTimeWindow(tenant, classId, now = new Date()) {
  const classItem = classById(tenant, classId);
  if (!classItem || classItem.openAccess) {
    return { allowed: true };
  }

  const sessions = classItem.sessions || [];
  if (!sessions.length) {
    return { allowed: false, reason: `Denied: ${classItem.name} has no scheduled session set by staff.` };
  }

  const currentDay = now.getDay();
  const currentMinutes = now.getHours() * 60 + now.getMinutes();
  const sessionsToday = sessions
    .filter((session) => Number(session.day) === currentDay)
    .sort((a, b) => timeToMinutes(a.time) - timeToMinutes(b.time));

  if (!sessionsToday.length) {
    return { allowed: false, reason: `Denied: ${classItem.name} is not scheduled today. Next session: ${nextSessionText(sessions, now)}.` };
  }

  const activeSession = sessionsToday.find((session) => {
    const startMinutes = timeToMinutes(session.time);
    const prepWindowStart = startMinutes - 15;
    const endMinutes = startMinutes + Number(session.durationMinutes || 60);
    return currentMinutes >= prepWindowStart && currentMinutes <= endMinutes;
  });

  if (activeSession) {
    return { allowed: true };
  }

  const upcomingSession = sessionsToday.find((session) => currentMinutes < timeToMinutes(session.time) - 15);

  if (upcomingSession) {
    return { allowed: false, reason: `Denied: ${classItem.name} opens from 15 minutes before ${upcomingSession.time}.` };
  }

  const finalSession = sessionsToday[sessionsToday.length - 1];
  const finalEndMinutes = timeToMinutes(finalSession.time) + Number(finalSession.durationMinutes || 60);

  return { allowed: false, reason: `Denied: ${classItem.name} finished at ${minutesToTime(finalEndMinutes)} today.` };
}

function checkDailyClassUsage(tenant, member, classId, now = new Date()) {
  const classItem = classById(tenant, classId);
  if (!classItem || classItem.openAccess) {
    return { allowed: true };
  }

  const alreadyUsed = tenant.accessLogs.some((log) => (
    log.allowed &&
    log.memberId === member.id &&
    log.visitId === classId &&
    sameAccessDay(log.createdAt, now)
  ));

  if (alreadyUsed) {
    return { allowed: false, reason: `Denied: ${member.name} has already used ${classItem.name} access today.` };
  }

  return { allowed: true };
}

function sameAccessDay(dateIso, now) {
  const date = new Date(dateIso);
  return date.getFullYear() === now.getFullYear() &&
    date.getMonth() === now.getMonth() &&
    date.getDate() === now.getDate();
}

function planScheduleText(tenant, plan) {
  const scheduleParts = plan.entitlements
    .map((classId) => classById(tenant, classId))
    .filter((classItem) => classItem && !classItem.openAccess)
    .flatMap((classItem) => (classItem.sessions || []).map((session) => `${classItem.name}: ${dayName(session.day)} ${session.time}`));

  return scheduleParts.length ? scheduleParts.join(", ") : "Open access";
}

function nextSessionText(sessions, now) {
  const today = now.getDay();
  const ordered = sessions
    .map((session) => ({ ...session, offset: (Number(session.day) - today + 7) % 7 }))
    .sort((a, b) => a.offset - b.offset || timeToMinutes(a.time) - timeToMinutes(b.time));
  const next = ordered[0];
  return `${dayName(next.day)} ${next.time}`;
}

function dayName(day) {
  return ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"][Number(day)] || "Day";
}

function timeToMinutes(time) {
  const [hours, minutes] = time.split(":").map(Number);
  return hours * 60 + minutes;
}

function minutesToTime(totalMinutes) {
  const hours = Math.floor(totalMinutes / 60) % 24;
  const minutes = totalMinutes % 60;
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}`;
}

function showDecision(element, type, message) {
  element.className = `decision ${type}`;
  element.textContent = message;
}

function createCredential(type) {
  if (type === "PIN") return String(Math.floor(1000 + Math.random() * 9000));
  return String(Math.floor(1000 + Math.random() * 9000));
}

function createSlug(value) {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "") || `class_${Date.now()}`;
}

function uniqueId(baseId, existingIds) {
  let candidate = baseId;
  let counter = 2;
  while (existingIds.includes(candidate)) {
    candidate = `${baseId}_${counter}`;
    counter += 1;
  }
  return candidate;
}

function addDaysIso(days) {
  const date = new Date();
  date.setDate(date.getDate() + Math.max(1, Number(days || 1)) - 1);
  date.setHours(23, 59, 59, 999);
  return date.toISOString();
}

function isExpired(dateIso) {
  return new Date(dateIso).getTime() < Date.now();
}

function daysUntil(dateIso) {
  const ms = new Date(dateIso).getTime() - Date.now();
  return Math.max(0, Math.ceil(ms / 86400000));
}

function formatDate(dateIso) {
  return new Intl.DateTimeFormat("en-GB", { day: "2-digit", month: "short", year: "numeric" }).format(new Date(dateIso));
}

function formatDateTime(dateIso) {
  return new Intl.DateTimeFormat("en-GB", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit"
  }).format(new Date(dateIso));
}

function loadState() {
  const saved = localStorage.getItem(STORAGE_KEY);
  if (!saved) return structuredClone(defaultState);
  try {
    return JSON.parse(saved);
  } catch {
    return structuredClone(defaultState);
  }
}

function cloneDefaultClasses() {
  return structuredClone(defaultClasses);
}

function saveState() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

render();
processCheckoutReturn();
