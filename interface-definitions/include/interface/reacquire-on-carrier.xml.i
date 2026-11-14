<!-- include start from interface/reacquire-on-carrier.xml.i -->
<leafNode name="reacquire-on-carrier">
  <properties>
    <help>Re-acquire the DHCP lease immediately when the interface carrier returns (NO-CARRIER to carrier), instead of waiting for the renewal timer. Useful downstream of a DOCSIS-style WWAN ip-passthrough that signals a carrier IP change by bouncing the LAN port.</help>
    <valueless/>
  </properties>
</leafNode>
<!-- include end -->
